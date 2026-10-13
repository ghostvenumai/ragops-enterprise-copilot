import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";
import de from "../../messages/de-DE.json";
import en from "../../messages/en-US.json";

type Tree = { [key: string]: string | Tree };

function flatten(tree: Tree, prefix = ""): Map<string, string> {
  const out = new Map<string, string>();
  for (const [key, value] of Object.entries(tree)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "string") out.set(path, value);
    else for (const [inner, text] of flatten(value, path)) out.set(inner, text);
  }
  return out;
}

const german = flatten(de as Tree);
const english = flatten(en as Tree);

/** Top-level ICU argument names, e.g. {count, plural, ...} -> count. */
function argumentsOf(message: string): string[] {
  return [...message.matchAll(/\{\s*([a-zA-Z_][\w]*)\s*(?:,|\})/g)].map((match) => match[1] as string).sort();
}

describe("translation catalogs", () => {
  it("have exactly the same keys in de-DE and en-US", () => {
    const missingInEnglish = [...german.keys()].filter((key) => !english.has(key));
    const missingInGerman = [...english.keys()].filter((key) => !german.has(key));
    expect(missingInEnglish).toEqual([]);
    expect(missingInGerman).toEqual([]);
  });

  it("contain no empty messages", () => {
    const empty = [...german, ...english].filter(([, text]) => !text.trim()).map(([key]) => key);
    expect(empty).toEqual([]);
  });

  it("use the same placeholders in both languages", () => {
    const mismatched = [...german.keys()].filter(
      (key) => JSON.stringify(argumentsOf(german.get(key) ?? "")) !== JSON.stringify(argumentsOf(english.get(key) ?? "")),
    );
    expect(mismatched).toEqual([]);
  });

  it("are actually translated (no German text copied into the English catalog)", () => {
    const umlaut = [...english].filter(([, text]) => /[äöüÄÖÜß]/.test(text)).map(([key]) => key);
    expect(umlaut).toEqual([]);
  });

  it("cover every module the briefing names", () => {
    for (const key of ["cockpit", "copilot", "knowledge", "intelligence", "monitoring", "finops", "governance"]) {
      expect(german.get(`nav.${key}`)).toBeTruthy();
      expect(english.get(`nav.${key}`)).toBeTruthy();
    }
  });
});

/** Guard against hardcoded UI text: JSX text nodes and user-facing string attributes, parsed with TypeScript. */
describe("no hardcoded user-facing strings", () => {
  const root = join(__dirname, "../../src");
  const allowedText = new Set(["RAGOps", "RAGOps Enterprise Copilot", "DE", "EN", "KB", "EUR", "v"]);
  const files: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (path.endsWith(".tsx")) files.push(path);
    }
  };
  walk(root);

  function literals(file: string): string[] {
    const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const found: string[] = [];
    const visit = (node: ts.Node) => {
      if (ts.isJsxText(node)) {
        const text = node.getText().trim();
        if (/[A-Za-zÄÖÜäöü]{2,}/.test(text) && !allowedText.has(text)) found.push(text);
      }
      if (ts.isJsxAttribute(node) && ["aria-label", "title", "placeholder", "alt", "label"].includes(node.name.getText())) {
        const value = node.initializer;
        if (value && ts.isStringLiteral(value) && /[A-Za-zÄÖÜäöü]{3,}/.test(value.text)) found.push(value.text);
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
    return found;
  }

  it("finds the component sources", () => {
    expect(files.length).toBeGreaterThan(20);
  });

  it.each(files.map((file) => [file.slice(root.length + 1), file]))("%s has no literal UI text", (_name, file) => {
    expect(literals(file as string)).toEqual([]);
  });
});

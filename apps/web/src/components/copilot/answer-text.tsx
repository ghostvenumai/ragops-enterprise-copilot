import * as React from "react";

/**
 * Minimal, safe rendering of the structure model answers use: paragraphs, "- " bullet lists
 * and **bold** spans. It never parses HTML and never creates links: every piece of the answer
 * ends up in a React text node, so markup in an answer is shown, not executed.
 */
function inline(text: string, keyPrefix: string): React.ReactNode[] {
  return text.split(/(\*\*[^*\n]+\*\*)/g).map((part, index) =>
    part.length > 4 && part.startsWith("**") && part.endsWith("**") ? (
      <strong key={`${keyPrefix}-${index}`}>{part.slice(2, -2)}</strong>
    ) : (
      <React.Fragment key={`${keyPrefix}-${index}`}>{part}</React.Fragment>
    ),
  );
}

export function AnswerText({ text }: { text: string }) {
  const blocks: React.ReactNode[] = [];
  let items: string[] = [];
  const flush = () => {
    if (items.length) {
      const key = `list-${blocks.length}`;
      blocks.push(
        <ul key={key} className="list-disc space-y-1 pl-5">
          {items.map((item, index) => (
            <li key={`${key}-${index}`}>{inline(item, `${key}-${index}`)}</li>
          ))}
        </ul>,
      );
      items = [];
    }
  };
  for (const line of text.split("\n")) {
    const bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
    if (bullet) {
      items.push(bullet[1] ?? "");
      continue;
    }
    flush();
    if (line.trim()) blocks.push(<p key={`p-${blocks.length}`}>{inline(line, `p-${blocks.length}`)}</p>);
  }
  flush();
  return <>{blocks}</>;
}

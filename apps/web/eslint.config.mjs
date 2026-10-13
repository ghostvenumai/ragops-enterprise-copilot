import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({ baseDirectory: dirname(fileURLToPath(import.meta.url)) });

const config = [
  { ignores: [".next/**", "node_modules/**", "coverage/**", "next-env.d.ts"] },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    rules: {
      // Document text must never become markup; the only sanctioned HTML sink is none.
      "react/no-danger": "error",
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: "Rendering HTML from data is forbidden in RAGOps.",
        },
        {
          // An effect may return only a cleanup function; an expression body can leak a
          // value (for example the Promise newer browsers return from scrollIntoView).
          selector: "CallExpression[callee.property.name='useEffect'] > ArrowFunctionExpression[body.type!='BlockStatement']",
          message: "Use a block body for effects so they never return a non-cleanup value.",
        },
        {
          selector: "CallExpression[callee.name='useEffect'] > ArrowFunctionExpression[body.type!='BlockStatement']",
          message: "Use a block body for effects so they never return a non-cleanup value.",
        },
      ],
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/consistent-type-imports": "error",
    },
  },
];

export default config;

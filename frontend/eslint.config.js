import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";

// Legacy business JSX stays unchanged; new/migrated TypeScript is gated.
export default tseslint.config(
  { ignores: ["dist/**", "node_modules/**"] },
  { files: ["**/*.{js,jsx}"], plugins: { "react-hooks": reactHooks }, languageOptions: { ecmaVersion: "latest", sourceType: "module", parserOptions: { ecmaFeatures: { jsx: true } } } },
  ...tseslint.configs.recommended.map((config) => ({ ...config, files: ["**/*.{ts,tsx}"] })),
  {
    files: ["src/**/*.{ts,tsx}", "test/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
      "@typescript-eslint/no-empty-object-type": "off",
    },
  },
);

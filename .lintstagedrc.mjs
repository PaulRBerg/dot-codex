/**
 * @type {import("lint-staged").Configuration}
 */
export default {
  "*": () => "just gitleaks-staged",
  "**/*.{md,json,jsonc,yaml,yml}":
    "bunx --no-install prettier --write --cache --cache-location .cache/prettier/.prettier-cache --log-level warn",
  "**/*.toml": "just toml-format-check",
  "**/*.py": ["uvx --from 'ruff>=0.15.18,<0.16' ruff check --fix", "uvx --from 'ruff>=0.15.18,<0.16' ruff format"],
};

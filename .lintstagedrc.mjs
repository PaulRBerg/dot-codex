/**
 * @type {import("lint-staged").Configuration}
 */
export default {
  "*": () => "just gitleaks-staged",
  "**/*.{md,json,jsonc,yaml,yml}":
    "bunx --no-install prettier --write --cache --cache-location .cache/prettier/.prettier-cache --log-level warn",
  "**/*.toml": "just toml-format-check",
};

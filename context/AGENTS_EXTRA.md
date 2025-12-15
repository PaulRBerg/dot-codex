## Skill Activation

When the user's prompt contains a `<skill_activation>` XML tag, search recursively for Markdown files in:

1. `~/.claude/skills`
2. `~/.claude/plugins/marketplaces/**/skills`

Example for `fp:effect-ts`:

```
~/.claude/plugins/marketplaces/sablier-plugins/plugins/fp/skills/effect-ts
```

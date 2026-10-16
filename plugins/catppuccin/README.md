# catppuccin

[Catppuccin](https://catppuccin.com) themes for Claude Code: Latte (light) and Frappé, Macchiato and Mocha (dark).

Run `/theme` after installing and pick one of the **Catppuccin** entries.

```text
/plugin marketplace add https://github.com/gaetschwartz/claude-plugins
/plugin install catppuccin@gaetans-claude-plugins
/reload-plugins
```

Colours come from the official palette (`@catppuccin/palette` 1.8.0). Each theme overrides the semantic
colours of its base (`light` for Latte, `dark` for the others): accents and status colours map to the
matching Catppuccin accents, and diff backgrounds are tinted from the flavour's green and red over its base.

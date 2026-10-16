# themes

Colour themes for Claude Code, selectable from `/theme`.

| Family | Entries | Base |
|--------|---------|------|
| Catppuccin | Latte, Frappé, Macchiato, Mocha | Latte light; the rest dark |
| Kanagawa | Wave, Dragon, Lotus | Lotus light; the rest dark |
| Rosé Pine | Rosé Pine, Moon, Dawn | Dawn light; the rest dark |

Each theme overrides the semantic colours of its base (`light` or `dark`). Colours come from the upstream palettes: `@catppuccin/palette` 1.8.0, `kanagawa.nvim` (`colors.lua` and `themes.lua`), and `@rose-pine/palette` 4.0.1. Accents and status colours map to the matching palette accents, and diff backgrounds come from each family's own diff colours or are tinted from the accent over the base.

Run `/theme` after installing and pick one of the entries.

```text
/plugin marketplace add https://github.com/gaetschwartz/claude-plugins
/plugin install themes@gaetans-claude-plugins
/reload-plugins
```

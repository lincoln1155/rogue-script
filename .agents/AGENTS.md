# Project-Scoped Rules

## rogue_lite.lua Modification Rule
When changing `rogue_lite.lua` or writing new features for it, ALWAYS check `rogue_ui.lua` first. `rogue_lite.lua` is a lighter version of `rogue_ui.lua`, designed to take core concepts from it and debloat other functions. You must verify if the problem you're solving or the feature you're adding has already been fixed/implemented in `rogue_ui.lua` before proceeding.

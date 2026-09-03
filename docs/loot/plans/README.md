# Loot Plans

This folder contains proposed, multi-step migrations for item-management
features. Plans are not current runtime contracts; confirm behavior in the
owning Python source, native bindings, and injected-client observations.

- `inventory-plus-to-system-settings.md` - proposed migration of Inventory+
  item features into `System Settings > Items & Merchants`; its migration
  journal records the first Colorize/Xunlai slice, verification, and resume
  point.
- `cross-account-inventory-transfer.md` - drop-and-collect item ferry
  that consolidates multiboxed inventories in an explorable area instead of
  running a trade window per pair; records the reuse map, the shared-memory
  message limits that shape its protocol, the slot/stack budgeting algorithm,
  and the game-behavior assumptions still awaiting live verification. Its
  offline planner, drop/collect yield helpers, shared-memory command handlers
  and dry-run widget (phases 1-4) are implemented; the widget previews a whole
  session, including the messages it would send, without sending any of them.

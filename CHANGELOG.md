# Changelog

All notable changes to this project are documented in this file.

## v2.2.0 - 2026-04-27

### Release Type
- **Bug fix release**

### Fixes
- Corrected MQTT topic paths to remove the leading slash.
- Topics now start with `ecl110/` instead of `/ecl110/`.

### Compatibility Implications
- **Potentially breaking for topic routing**: Any subscriber, automation, or publisher still using topics that begin with `/ecl110/` must be updated to `ecl110/`.
- The legacy JSON topic `ecl110/ecl110_data` remains available for compatibility.
- No protocol, payload format, or register mapping changes were introduced in this release.

## v2.0.0 - 2026-04-23

### Overview
This release introduces a major architectural refactor of the ECL110 MQTT integration service. The service now runs as an autonomous polling bridge with change-aware publishing and hierarchical MQTT topics.

### New Features
- **Autonomous internal polling loop**
  - Replaced command-triggered measurement with an internal timer loop
  - Polls Modbus registers every 60 seconds (`LOOP_TIME = 60`)
  - Starts polling immediately after successful startup

- **Change detection publishing model**
  - Added in-memory value cache (`previous_values`)
  - Publishes MQTT updates only when values change
  - Reduces MQTT traffic and downstream processing load

- **Hierarchical MQTT topic model**
  - Added per-point topics under `ecl110/...`
  - Example topics:
    - `ecl110/flow_temp_control/slope`
    - `ecl110/flow_temp_control/displace`
    - `ecl110/temperatures/outdoor_temp`
    - `ecl110/return_temp_limit/limit`

- **Writable `/set` topics**
  - Added write support via MQTT topics ending in `/set`
  - Supports both payload styles:
    - Plain numeric payload (`2`)
    - JSON payload (`{"value":2}`)
  - Example write topic:
    - `ecl110/flow_temp_control/displace/set`

- **Legacy JSON compatibility channel retained**
  - Continues publishing full JSON payload on `ecl110/ecl110_data`
  - Preserves compatibility for existing subscribers

### Behavior Changes
- The service no longer depends on receiving `{"command":"measure"}` messages to perform reads.
- Telemetry publishing is now event-like (change-based) instead of unconditional per trigger.
- Legacy JSON topic is emitted when at least one value changes.

### Breaking Changes
- **Primary data contract changed** from single command/response pattern to autonomous polling + hierarchical topics.
- Integrations that relied on command-topic triggering (`ecl110/command`) as the main control flow must be updated.

### Migration Notes (from previous version)
1. **Consumers of telemetry**
   - Preferred: subscribe to `ecl110/#` and process individual point topics.
   - If needed, continue consuming `ecl110/ecl110_data` with existing JSON parser.

2. **Writers / control commands**
   - Replace command-based write messages with direct `/set` topic writes.
   - Example migration:
     - Old: publish `{"command":"displace","value":2}` to `ecl110/command`
     - New: publish `2` (or `{"value":2}`) to `ecl110/flow_temp_control/displace/set`

3. **Automation and dashboards**
   - Update topic subscriptions and entity mappings to new hierarchy.
   - Account for change-only publishing behavior (no periodic duplicate values when stable).

4. **Operational monitoring**
   - Confirm polling cadence is acceptable for your use case (60 seconds default).
   - Validate watchdog behavior in environments where repeated communication loss can occur.

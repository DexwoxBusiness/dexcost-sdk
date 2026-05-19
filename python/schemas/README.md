# Dexcost Standard Event Schema

## Version: 1

This directory contains the JSON Schema (Draft-07) definitions for the
Dexcost Standard Event Schema v1. These schemas define the contract for
Task and Event payloads used throughout the dexcost SDK.

## Files

| File | Description |
|------|-------------|
| `dexcost-task.v1.json` | JSON Schema for Task payloads |
| `dexcost-event.v1.json` | JSON Schema for Event payloads |

## Compatibility Contract

The following rules govern schema evolution within a major version:

1. **Additive only** -- New fields may be added with sensible defaults.
   Consumers must tolerate unknown fields in transit even though the
   schema sets `additionalProperties: false` for validation.

2. **No removal** -- Existing fields are never removed within v1.x.

3. **No type changes** -- The type of an existing field is never changed
   within a major version. A field that is `string` stays `string`.

4. **Deprecation requires version bump** -- If a field must be removed or
   its type changed, a new major schema version (v2) is required. The
   old version continues to be supported for a documented migration
   period.

5. **`schema_version` is always present** -- Every payload carries a
   `schema_version` field (`"1"` for this version) so consumers can
   dispatch to the correct validation logic.

## Validation

Use `dexcost.validate(payload)` to validate a dictionary against the
appropriate v1 schema. The function returns an empty list on success or a
list of human-readable error strings.

```python
from dexcost import validate

errors = validate(task.to_dict())
assert errors == []
```

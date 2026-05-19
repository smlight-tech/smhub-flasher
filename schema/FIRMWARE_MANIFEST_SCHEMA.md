# SMHUB Firmware Manifest Schema

This document dictates the structure of the firmware manifest (`firmware.json`) utilized by the `smhub-flasher` tool (for both the CLI and the GUI wrapper). 

## JSON Payload Structure

A valid manifest should follow this structure natively:

```json
{
  "$schema": "https://smlight.tech/schemas/smhub-firmware-v1.json",
  "manifest_version": 1,
  "generated_at": "2026-04-17T23:00:00Z",
  "product": "smhub",
  "releases": [
    {
      "version": "0.9.9",
      "channel": "stable",
      "released_at": "2026-04-05T14:18:06Z",
      "notes": "Stable release.",
      "notes_url": null,
      "requires_flasher": ">=0.1.0",
      "fastboot": true,
      "artifacts": {
        "firmware": {
          "url": "https://updates.smlight.tech/firmware/smhub/os/smhub_os_v0.9.9.zip",
          "size_bytes": 300710465,
          "sha256": "b46acdf437370e1b0856320fe1f04825a062c09624ff94d7f763397e619ad8f1"
        }
      }
    }
  ]
}
```

### Root Properties

* `$schema`: Standard JSONSchema definition link.
* `manifest_version`: Integer representation. Currently `1`.
* `generated_at`: ISO timestamp indicating when the CI pipeline compiled the manifest.
* `product`: Hardcoded targeting tag. Expected to match `"smhub"`.
* `releases`: An array of officially deployed firmware versions.

### Release Object Properties

* `version`: A distinct string representation (e.g. `"1.0.0.dev3"`, `"0.9.9"`).
* `channel`: Defines the deployment category natively. For example: `"stable"`, `"beta"`, `"alpha"`. This effectively drives UI dropdown mapping automatically.
* `released_at`: Standard ISO timestamp. Used for UI chronologies and sorting `latest` branches natively downstream.
* `notes`: Optional string to inject into GUIs outlining changelog highlights.
* `notes_url`: Nullable HTTP reference pointing to detailed technical release bulletins.
* `requires_flasher`: Semantic version requirement for the flasher tool.
* `fastboot`: `true` or `false` boolean. Dictates whether standard Android Fastboot protocol initialization is used, overriding internal legacy CVI routines. 
* `artifacts`: Key-value dictionaries pinning raw URLs and overall zip-level SHA256 integrity validators.



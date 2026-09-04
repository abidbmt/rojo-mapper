# rojo-mapper

`rmp` (rojo-mapper) generates one deterministic root `default.project.json` for an opinionated multi-place Roblox source tree. It does not generate or own `sourcemap.json`.

## Status

Version 0.1 is a clean-cut, single-active-target workflow. Generated manifests are consumer artifacts and should remain ignored.

## Install

Release archives are standalone, unsigned binaries for Windows x64, Linux x64, macOS x64, and macOS arm64. Verify the published SHA-256 manifest and GitHub artifact attestation before running. Windows SmartScreen or macOS Gatekeeper may show an unsigned-publisher trust prompt.

With mise, define an alias for `github:<owner>/rojo-mapper`; asset names include the operating system and architecture and archives contain one root directory suitable for mise root stripping. This project is not published to PyPI and does not support pipx installation.

For source development:

```text
mise install
mise run sync
mise run lint
mise run test
```

Development and `dev` use exact Rojo 7.6.1 from `mise.lock`. The compatibility gate also accepts fixed releases `>=7.7.1,<7.8`; broken 7.7.0 is rejected.

## Project layout

A consuming project has `Source/`. Direct children other than `Places` are common logical roots. Direct children of `Source/Places/` are places. Context segments map as follows:

| Context | DataModel target |
| --- | --- |
| `Shared` | `ReplicatedStorage.Shared` |
| `Server` | `ServerScriptService.Server` |
| `Client` | `ReplicatedStorage.Client` |
| `First` | `ReplicatedFirst.First` |

Common logical roots keep their name as a runtime layer. A selected place uses the runtime layer `Place`.

```toml
schema = 1
ignore = ["Source/**/*.spec.luau"]

[static]
Packages = "ReplicatedStorage.Packages"

[cloud]
universe_id = 999

[cloud.places]
Common = 100
Main = 111
Place2 = 222
```

Static sources are opaque common directory mounts. Ignore syntax is deliberately portable: literals, `*` within one segment, and whole-segment `**` only.

## Commands

```text
rmp list [--format human|json]
rmp validate [Common|Place] [--format human|json]
rmp generate [Common|Place] [--format human|json]
rmp dev [Common|Place]
```

`generate` and `dev` infer the target only when exactly one place exists. `validate` without a target validates `Common` and all places without writing. `list` never writes.

`dev` supervises one `rojo serve default.project.json` and structurally watches `Source/`, `rojo-mapper.toml`, and static roots. Mapping/config-invalid snapshots retain the last valid session. Tree-only manifest changes do not restart Rojo. Session metadata changes stop Rojo before replacing the manifest, then start a fresh session. That controlled restart disconnects Studio; reconnect the Rojo plugin manually to `localhost:34872` when the `dev.rojo_restarted_reconnect_required` notice appears.

## Luau Language Server

Use a trusted VS Code workspace. GUI-launched VS Code must receive persistent PATH entries for both mise shims and `rojo`. Configure Luau-LSP:

```json
{
  "luau-lsp.sourcemap.autogenerate": true,
  "luau-lsp.sourcemap.generatorCommand": "",
  "luau-lsp.sourcemap.useVSCodeWatcher": false,
  "luau-lsp.sourcemap.rojoProjectFile": "default.project.json",
  "luau-lsp.sourcemap.sourcemapFile": "sourcemap.json",
  "luau-lsp.sourcemap.includeNonScripts": true
}
```

This delegates sourcemap ownership to long-running `rojo sourcemap --watch`. Without `dev`, rerun `generate <target>` after structural source changes.

## Feature exposure

Below `Features/**`, a script or data module directly inside its context is prefixed with the immediate feature owner. `Features/Combat/Shared/API.luau` becomes `CombatAPI`; `Features/Combat/Shared/Internal/Codec.luau` remains `Internal.Codec`. Models are never prefixed.

## License

MIT.

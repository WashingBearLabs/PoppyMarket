# PoppyMarket

The app registry for [Poppy](https://github.com/WashingBearLabs/Poppy) — a personal AI operating system by WashingBearLabs.

## What is this?

PoppyMarket is a simple JSON registry that Poppy fetches to discover available apps. When you open the "App Store" page in Poppy, it pulls `registry.json` from this repo to show you what apps are available for one-click installation.

## Registry Format

`registry.json` contains an array of app entries. Each entry describes an app with enough metadata for Poppy to display it in the UI and for users to install it.

### Required Fields

| Field | Description |
|-------|-------------|
| `name` | Unique identifier (lowercase, DNS-label format) |
| `description` | Short description (shown on app card) |
| `version` | Current version (semver) |
| `author` | Publisher name |
| `repo_url` | Git repository URL (HTTPS) — used for clone-and-build installs |

### Discovery Fields

These help Poppy recommend apps based on user intent:

| Field | Description |
|-------|-------------|
| `long_description` | Detailed description (embedding-friendly, natural language) |
| `capabilities` | Structured list of what the app can do |
| `tags` | Searchable keywords |
| `use_cases` | Natural language examples of when you'd use this app |

### Optional Fields

| Field | Description |
|-------|-------------|
| `image` | Pre-built Docker image reference (skips clone-and-build) |
| `icon` | URL to app icon |
| `category` | Top-level grouping (development, productivity, security, etc.) |
| `port` | Container internal port (default: 8000) |
| `manifest_url` | URL to fetch the full app manifest from the app's repo |

## Adding an App

1. Fork this repo
2. Add your app entry to the `apps` array in `registry.json`
3. Submit a PR

Your app must have a valid `manifest.yaml` and follow the [Poppy App SDK](https://github.com/WashingBearLabs/Poppy) conventions.

## Available Apps

| App | Description | Author |
|-----|-------------|--------|
| [Acorn Atelier](https://github.com/WashingBearLabs/Acorn) | Visual development environment for managing software projects | WashingBearLabs |

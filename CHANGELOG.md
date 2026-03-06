# Changelog

All notable changes to the Ellya skill will be documented in this file.

## [Unreleased]

### Added
- **AI-Powered Series Generation**: New `series` command that intelligently generates photo series from a single base image
  - Automatic scene classification: AI analyzes the image and chooses between story mode or pose mode
  - **Story Mode**: Generates narrative photo sequences with logical progression
    - Creates realistic, physically possible story scenes
    - Includes photographer's perspective with camera angles, body postures, facial expressions
    - Validates variations to ensure concrete, actionable descriptions
  - **Pose Mode**: Generates technical photography variations
    - Different camera angles (front-facing, three-quarter, side profile, overhead, low-angle)
    - Various body postures and expressions
    - Maintains scene consistency while varying composition
  - Configurable count parameter (`-n/--count`) to specify number of images (default: 3, max: 10)
  - Automatic scene and character extraction from base image
  - Context generation (story plot or scene summary) for better coherence

### Changed
- **Decoupled Media Sending**: Removed `send_media()` function from generation scripts
  - Scripts now focus solely on image generation
  - Media sending is handled by OpenClaw through skill handler
  - Removed CLI parameters: `-c/--channel`, `-t/--target`, `-msg/--message`
  - Simplified function signatures across all generation methods

### Fixed
- Prompt placeholder replacement: Fixed double braces `{{}}` to single braces `{}` for proper `.format()` substitution
- Count parameter validation: Added 1-10 range validation with proper error messages
- Array overflow protection: Implemented modulo cycling when count exceeds default variation list length
- Resource management: Added proper file handle closing using `with` statements
- Exception handling: Added `FileNotFoundError` catch for missing dependencies

### Improved
- Unified prompt management: All prompts defined as constants at file header
- Code quality: Translated all Chinese comments and docstrings to English
- Default variations: Updated with more concrete, specific descriptions including camera angles
- Validation logic: Added `is_valid_story_variation()` to filter abstract or unrealistic descriptions

## [Previous Versions]

Initial release with basic image generation and analysis capabilities.

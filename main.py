"""Source-checkout entry point; installed users can run mca-editor."""

from mca_editor.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

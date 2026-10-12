# Integration branding

`icon.svg` is the editable source for this project's integration icon. It uses the shared dark surface (`#292b40`), pale blue (`#d9dffd`), lavender (`#c4aff5`) and muted blue (`#a0a9cc`) palette.

Export this SVG as transparent RGBA PNG at 256 × 256 and 512 × 512 pixels into `custom_components/robot_cleaner_queue/brand/icon.png` and `icon@2x.png`, respectively. Keep the PNGs committed: Home Assistant reads these images directly; it does not render the source SVG.

The installed `brand/` directory is discovered automatically by Home Assistant 2026.3 and later. It ships in the existing HACS integration layout. Older Home Assistant versions continue to work but do not display this bundled branding.

Reference: https://developers.home-assistant.io/docs/core/integration/brand_images/

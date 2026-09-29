`saridis-logo.png` is the user-supplied transparent, optimized PNG prepared from
the actual `saridis-logo.pdf` (420 × 94 pixels, 9,570 bytes). The supplied base64
was decoded directly without altering the image.

The header loads `/static/assets/saridis-logo.png?v=ui2` and preserves its aspect
ratio. Flask serves the same asset with or without a cache-busting query string.
The existing image-error handler shows the hidden SARIDIS text fallback only if
the image fails to load.

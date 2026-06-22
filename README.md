# gabriel.gasparolo.com

Static personal website for `https://gabriel.gasparolo.com`.

Design notes:

- Minimal personal-site structure inspired by `annamona.co`.
- Dracula Classic palette from `https://draculatheme.com/spec`.
- Public professional positioning only: no personal-life details and no internal company specifics. Current employer is named because Gabriel asked for it explicitly.
- LinkedIn profile/activity was checked from Gabriel's open Chrome window. The site uses only public, broad professional themes.

Open locally:

```sh
python3 -m http.server 8080
```

Regenerate:

```sh
python3 generate_site.py
```

Publish:

- Cloudflare Pages project: `gabriel-gasparolo-com`
- Build command: none
- Output directory: `public`
- Custom domain: `gabriel.gasparolo.com`

For fresh LinkedIn posts, keep the LinkedIn recent activity page open in the OpenClaw personal Chrome profile exposed on CDP port `18803`. If CDP or LinkedIn is unavailable, the script uses the curated fallback posts.

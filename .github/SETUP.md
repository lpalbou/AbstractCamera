# GitHub Actions setup

## PyPI Trusted Publishing

Configure a PyPI trusted publisher for this repository (project must exist on PyPI first):

- **PyPI project**: `abstractcamera`
- **Owner**: `lpalbou`
- **Repository**: `AbstractCamera`
- **Workflow name**: `Release`
- **Environment name**: `pypi`

The workflow uses OIDC (`pypa/gh-action-pypi-publish`). A failed publish with
`invalid-publisher` means this publisher record is missing or the environment
name does not match exactly.

Then add a GitHub environment named `pypi` in this repository's settings. The release workflow publishes through OIDC (`pypa/gh-action-pypi-publish`).

GitHub Release artifacts and GitHub Pages docs deploy even when PyPI publish is still pending setup.

## GitHub Pages

The release workflow deploys documentation with `mkdocs gh-deploy` after each tagged release.

One-time setup in the repository settings:

1. Open **Settings → Pages**
2. Set **Build and deployment → Source** to **Deploy from a branch**
3. Select branch **`gh-pages`** and folder **`/ (root)`**

The site URL is configured in `mkdocs.yml` as `https://www.lpalbou.info/AbstractCamera/`.

## Repository metadata

Set the repository description and topics for searchability:

```bash
gh repo edit lpalbou/AbstractCamera \
  --description "Multi-camera control for Python — tethered PTP bodies (Nikon, Sony), webcams, DWARF telescopes, and AbstractCore AI tool integration." \
  --add-topic camera --add-topic python --add-topic gphoto2 --add-topic webcam \
  --add-topic photography --add-topic abstractframework --add-topic abstractcore \
  --add-topic ptp --add-topic tethering --add-topic astrophotography --add-topic ai-tools
```

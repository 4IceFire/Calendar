# Bundled browser dependencies

These files are committed so TDeck pages do not require public internet access at runtime.

## Marked 12.0.2

- Project: https://github.com/markedjs/marked
- Package: https://registry.npmjs.org/marked/-/marked-12.0.2.tgz
- Bundled file: `marked/marked.min.js`
- SHA-256: `15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894`
- License: MIT; see `marked/LICENSE.md`

## DOMPurify 3.2.6

- Project: https://github.com/cure53/DOMPurify
- Package: https://registry.npmjs.org/dompurify/-/dompurify-3.2.6.tgz
- Bundled file: `dompurify/purify.min.js`
- SHA-256: `89e1fa7647cb495370d3a997ace4387f5d15d9f4c5af12352c53daa400956287`
- License: Apache-2.0 OR MPL-2.0; see `dompurify/LICENSE`

When updating either dependency, pin the new version, replace its upstream license,
record the new file hash here, and run `tests/test_web_performance.py`.

# Contribuir

¡Gracias! Unas pocas reglas:

1. **Nada real en el repo.** Ni credenciales, ni tokens, ni montos, saldos, posiciones u órdenes de una cuenta de
   verdad, ni en código, tests, issues, capturas o logs pegados. Para ejemplos usá el modo demo (`CAUCIONETTA_DEMO=1`).
2. **Tests sin red.** `pytest -q` corre aislado (sin red, sin Keychain, con su propio directorio de estado).
   Todo cambio en el bot o en el cliente viene con su test; todo endpoint nuevo, con uno en `tests/test_safety.py`.
3. **Seguridad primero.** No cambies los defaults a "en vivo", no saques el `dry=True` por defecto, no agrandes la
   lista cerrada de endpoints sin discutirlo en un issue.
4. **Estilo.** `ruff check .` limpio. Biblioteca estándar en runtime. Textos para el usuario en español
   rioplatense; código y comentarios en inglés.

```sh
uv venv && uv pip install -e ".[dev]"
pytest -q && ruff check .
```

## Capturas del README

Siempre desde el modo demo:

```sh
uv pip install playwright && playwright install chromium-headless-shell
python scripts/render_swiftbar.py     # docs/img/swiftbar-{light,dark}.png + swiftbar-output.txt
python scripts/render_terminal.py     # docs/img/terminal.svg
menubar/app/build.sh
CAUCIONETTA_DEMO=1 CAUCIONETTA_STATE_DIR=$(mktemp -d) sh -c 'caucionetta demo >/dev/null && caucionetta bot once >/dev/null && cp "$CAUCIONETTA_STATE_DIR/status.json" menubar/app/fixture-demo.json'
for m in "" --dark; do menubar/app/build/CaucionettaBar.app/Contents/MacOS/CaucionettaBar --snapshot docs/img/app-panel${m:+-dark}.png --fixture menubar/app/fixture-demo.json $m; done
```

## Cambiar el nombre

El nombre sale de `APP_NAME` en `caucionetta/__init__.py` y de `pyproject.toml`. Para renombrar todo (paquete,
comando, rutas, servicio del Keychain, label de launchd, app):

```sh
python scripts/rename.py nuevonombre        # muestra los cambios; agregá --apply para hacerlos
```

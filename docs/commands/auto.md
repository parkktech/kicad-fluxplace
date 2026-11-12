# Autorouting (`auto`)

## Autorouting

fluxplace optimizes for exactly what an autorouter wants — short, uncrossed nets. Workflow:

1. `place` the board with fluxplace.
2. Export Specctra DSN (`pcbnew.ExportSpecctraDSN`) and route with
   [Freerouting](https://github.com/freerouting/freerouting) headless
   (`java -jar freerouting.jar -de in.dsn -do out.ses -gui.enabled=false`).
3. Import the `.ses` back into KiCad.

Lower HPWL from `eval` correlates directly with higher autoroute completion.

# projects/

Hier legt die Werkbank die veroeffentlichten HTML-Versionen ab: `projects/<projekt-id>/v/001.html`,
`002.html`, ... Die Dateien sind Kopien der in `.local/werkbank/werkbank.sqlite3` gespeicherten
Versionen und werden nie ueberschrieben. Manuell hinzugefuegte Dateien mit dem gleichen Namensschema
werden beim Start oder ueber `POST /api/sync` eingelesen. Optional beschreibt `projects/<id>/projekt.json`
(`{"name": "Anzeigename"}`) ein Projekt, das nur als Dateien vorliegt.

Inhalte in diesem Ordner gehoeren nicht ins Git-Repository der Werkbank.

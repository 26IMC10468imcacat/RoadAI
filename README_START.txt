ROADAI WEB v3

ONLINE-DEPLOYMENT – STREAMLIT COMMUNITY CLOUD
=============================================

1. Lade den Inhalt dieses Ordners in ein GitHub-Repository.
   Wichtig: app.py, road_ai_core.py und requirements.txt müssen im Repository liegen.

2. Öffne:
   https://share.streamlit.io

3. Mit GitHub anmelden/verbinden.

4. Create app -> Yup, I have an app.

5. Repository auswählen.
   Branch: main
   Main file / Entrypoint: app.py

6. Advanced settings:
   Python 3.12

7. Deploy.

Die App erhält eine öffentliche Adresse:
https://<dein-name>.streamlit.app


LOKAL TESTEN
============
PowerShell in diesem Ordner:

py -m pip install -r requirements.txt
py -m streamlit run app.py


HINWEISE
========
- RoadAI v3 benötigt keine reale Fahrbahnbreite für die Prozentbewertung.
- HEIC wird in Streamlit Cloud serverseitig mit pillow-heif in JPG umgewandelt.
- Auf Windows bleibt die bestehende Windows-HEIC-Lösung des Cores nutzbar.
- SciPy und scikit-image werden nicht benötigt.

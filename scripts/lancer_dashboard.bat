@echo off
REM lancer_dashboard.bat — Dashboard de telemetrie du robot G1, EN UNE COMMANDE (Windows).
REM A executer sur TON ORDINATEUR Windows (double-clic ou depuis cmd), pas sur le robot.
REM
REM Robot sur une autre IP ? :  set G1_ROBOT_URL=http://192.168.0.128:8000  puis lancer ce .bat

if "%G1_ROBOT_URL%"=="" set G1_ROBOT_URL=http://192.168.123.164:8000
echo -^> Robot cible : %G1_ROBOT_URL%

python -c "import streamlit, pandas" 2>NUL
if errorlevel 1 (
  echo -^> Installation de streamlit + pandas...
  python -m pip install --quiet --upgrade streamlit pandas
)

echo -^> Ouverture du dashboard dans ton navigateur...
python -m streamlit run "%~dp0..\dashboard_stats.py"

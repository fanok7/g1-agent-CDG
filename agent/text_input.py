"""
agent/text_input.py — File d'attente partagée pour le chat clavier (fallback
micro). Alimentée par le thread terminal (main.py), consommée par
agent.events.text_input_loop qui l'injecte dans la conversation Realtime
exactement comme si le micro l'avait captée.
"""

import queue

input_queue = queue.Queue()

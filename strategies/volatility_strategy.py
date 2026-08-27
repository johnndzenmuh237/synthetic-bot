"""strategies/volatility_strategy.py"""
from strategy import evaluate, should_exit
SUITED = ["R_10","R_25","R_50","R_75","R_100","1HZ10V","1HZ25V","1HZ50V","1HZ75V","1HZ100V"]
def run(m15,h1,h4,symbol):
    return evaluate(m15,h1,h4,symbol) if symbol in SUITED else None
def exit_check(m15,h1,direction):
    return should_exit(m15,h1,direction)

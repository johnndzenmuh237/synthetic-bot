"""strategies/martingale.py — NOT active"""
class MartingaleManager:
    def __init__(self,base=1.0,steps=5): self.base=base; self.steps=steps; self.streak=0
    def get_stake(self,bal): return min(self.base*(2**self.streak),bal*0.05)
    def record_win(self): self.streak=0
    def record_loss(self): self.streak+=1

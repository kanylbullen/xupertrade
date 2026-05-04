"""HODL — long-term accumulation signals.

These are NOT trading strategies. They emit advisory signals telling a
human "now might be a good time to add more to your spot stack." They
do not place orders. The dashboard /hodl page surfaces current state.

Add a new signal by subclassing Signal in a new module under hodl/ and
calling @register on the class. It auto-loads via load_all().
"""

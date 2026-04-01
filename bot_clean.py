import time
import random

class PaperTradingBot:
    def __init__(self, initial_balance=1000):
        self.balance = initial_balance
        self.position = 0
        self.trade_history = []

    def buy(self, amount, price):
        cost = amount * price
        if cost <= self.balance:
            self.position += amount
            self.balance -= cost
            self.trade_history.append(('buy', amount, price))
            print(f'Bought {amount} at {price}.')
        else:
            print('Not enough balance to buy.')

    def sell(self, amount, price):
        if amount <= self.position:
            self.position -= amount
            self.balance += amount * price
            self.trade_history.append(('sell', amount, price))
            print(f'Sold {amount} at {price}.')
        else:
            print('Not enough position to sell.')

    def get_balance(self):
        return self.balance + (self.position * self.current_price())

    def current_price(self):
        return random.uniform(100, 200)  # Simulates a fluctuating price

    def trade(self):
        while True:
            price = self.current_price()
            if random.random() < 0.5:
                amount_to_buy = random.uniform(0.1, 10)
                self.buy(amount_to_buy, price)
            else:
                amount_to_sell = random.uniform(0.1, 10)
                self.sell(amount_to_sell, price)
            time.sleep(1)  # Simulates waiting time between trades

if __name__ == '__main__':
    bot = PaperTradingBot()
    bot.trade()
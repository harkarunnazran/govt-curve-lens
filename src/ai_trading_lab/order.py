

from typing import Literal, Optional
import datetime as dt

class Order:

    def __init__(
        self,
        symbol: str,
        quantity: int,
        order_type: Literal["buy", "sell"],
        price: float,
        time: dt.datetime,
        trader_id: int,
        
    ):
        self.symbol = symbol
        self.quantity = quantity
        self.order_type = order_type
        self.price = price
        self.time = time
        self.trader_id = trader_id
        self.order_priority: Optional[int] = None # This will be set when the order is added to the order book

        
        
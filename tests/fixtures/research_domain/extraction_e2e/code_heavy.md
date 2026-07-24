# API Reference

```python
def calculate_total(items):
    """Calculate total from list of items."""
    total = 0
    for item in items:
        total += item.price * item.quantity
    return total

class Item:
    def __init__(self, price, quantity):
        self.price = price
        self.quantity = quantity
```

```javascript
function fetchData(url) {
    return fetch(url)
        .then(response => response.json())
        .then(data => data.results);
}
```

This is a brief description of the API.

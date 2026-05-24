def total(values):
    # Intentional review fixture: handles empty list poorly.
    if not values:
        return 0.0
    return sum(values) / len(values)

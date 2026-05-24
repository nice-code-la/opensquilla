def main():
    rows = []  # Empty in some edge case
    if len(rows) < 4:
        return None
    return rows[3]['name'].lower()

if __name__ == '__main__':
    main()

import sqlite3

DATABASE = 'fingerprint_data.db'

def view_database():
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute('SELECT id, username, template FROM fingerprints')
        rows = cursor.fetchall()
        conn.close()

        if rows:
            print(f"{'ID':<5} {'Username':<20} {'Template (Hex)':<}")
            print("-" * 100)
            for row in rows:
                template_hex = row[2].hex()  # Full fingerprint template in hex format
                print(f"{row[0]:<5} {row[1]:<20} {template_hex:<}")
        else:
            print("No records found in the database.")

    except sqlite3.Error as e:
        print(f"Error accessing the database: {e}")

if __name__ == '__main__':
    view_database()

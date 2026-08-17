import sqlite3

DB_NAME = "betterwallet.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def _column_exists(conn, table, column):
    cursor = conn.execute(f"PRAGMA table_info({table})")
    columns = [row['name'] for row in cursor.fetchall()]
    return column in columns

def init_bettertrust_tables():
    conn = get_db_connection()

    conn.execute('''
        CREATE TABLE IF NOT EXISTS verification_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            full_name TEXT NOT NULL,
            business_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT NOT NULL,
            address TEXT NOT NULL,
            id_document TEXT,
            business_document TEXT,
            selfie_document TEXT,
            payment_status TEXT DEFAULT 'pending',
            verification_status TEXT DEFAULT 'pending',
            admin_notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    conn.commit()
    conn.close()

def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    if not _column_exists(conn, 'users', 'profile_photo'):
        conn.execute('ALTER TABLE users ADD COLUMN profile_photo TEXT')

    conn.commit()
    conn.close()

def init_cac_tables():
    conn = get_db_connection()

    conn.execute('''
        CREATE TABLE IF NOT EXISTS cac_registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            registration_type TEXT NOT NULL,

            full_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT NOT NULL,

            business_name_1 TEXT,
            business_name_2 TEXT,
            nature_of_business TEXT,
            business_address TEXT,

            trademark_class TEXT,
            trademark_type TEXT,
            goods_services_description TEXT,

            bvn TEXT,
            bank_name TEXT,
            bank_account_number TEXT,
            tin_number TEXT,

            id_document TEXT,
            passport_photo TEXT,
            mark_logo_document TEXT,
            cac_certificate_document TEXT,

            payment_status TEXT DEFAULT 'pending',
            application_status TEXT DEFAULT 'pending',
            admin_notes TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    if not _column_exists(conn, 'cac_registrations', 'cac_status_report'):
        conn.execute('ALTER TABLE cac_registrations ADD COLUMN cac_status_report TEXT')

    conn.commit()
    conn.close()

def init_staffhook_tables():
    conn = get_db_connection()

    conn.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employer_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            category TEXT NOT NULL,
            location TEXT NOT NULL,
            pay_rate TEXT NOT NULL,
            pay_type TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (employer_id) REFERENCES users (id)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            applicant_id INTEGER NOT NULL,
            cover_message TEXT,
            status TEXT DEFAULT 'pending',
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs (id),
            FOREIGN KEY (applicant_id) REFERENCES users (id)
        )
    ''')

    if not _column_exists(conn, 'applications', 'viewed_by_employer'):
        conn.execute('ALTER TABLE applications ADD COLUMN viewed_by_employer INTEGER DEFAULT 0')
    if not _column_exists(conn, 'applications', 'viewed_by_applicant'):
        conn.execute('ALTER TABLE applications ADD COLUMN viewed_by_applicant INTEGER DEFAULT 0')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS worker_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            full_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            skill TEXT NOT NULL,
            bio TEXT,
            location TEXT NOT NULL,
            photo TEXT,
            is_featured INTEGER DEFAULT 0,
            is_admin_posted INTEGER DEFAULT 0,
            verified_by_betterwallet INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_db()
    init_staffhook_tables()
    init_bettertrust_tables()
    init_cac_tables()
    print("Database initialized successfully.")
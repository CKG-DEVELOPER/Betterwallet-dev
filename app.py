from flask import Flask, request, jsonify, render_template, session, redirect
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
import os
import random
import time
import requests

load_dotenv()

from chatbot import get_chat_reply

from database import get_db_connection, init_db, init_staffhook_tables, init_bettertrust_tables

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-this")
CORS(app, supports_credentials=True)

init_db()
init_staffhook_tables()
init_bettertrust_tables()

@app.route('/cac')
def cac_hub():
    if 'user_id' not in session:
        return redirect('/login')

    return render_template('cac.html')

@app.route('/bettertrust/admin')
def bettertrust_admin():
    if 'user_id' not in session:
        return redirect('/login')

    admin_email = os.getenv('ADMIN_EMAIL', '')
    is_admin = session.get('user_email', '').lower() == admin_email.lower()

    if not is_admin:
        return jsonify({"error": "You are not authorized to view this page."}), 403

    conn = get_db_connection()
    verifications = conn.execute('''
        SELECT * FROM verification_requests
        WHERE verification_status = 'pending'
        ORDER BY created_at DESC
    ''').fetchall()
    conn.close()

    return render_template('bettertrust-admin.html', verifications=verifications)

@app.route('/bettertrust/admin/update/<int:verification_id>', methods=['POST'])
def bettertrust_admin_update(verification_id):
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    admin_email = os.getenv('ADMIN_EMAIL', '')
    is_admin = session.get('user_email', '').lower() == admin_email.lower()

    if not is_admin:
        return jsonify({"error": "You are not authorized to do this."}), 403

    data = request.json
    new_status = data.get('status', '').strip()

    if new_status not in ('approved', 'rejected'):
        return jsonify({"error": "Invalid status."}), 400

    conn = get_db_connection()
    conn.execute(
        'UPDATE verification_requests SET verification_status = ? WHERE id = ?',
        (new_status, verification_id)
    )
    conn.commit()
    conn.close()

    return jsonify({"message": f"Verification {new_status}."}), 200

@app.route('/bettertrust/verify-payment')
def bettertrust_verify_payment():
    if 'user_id' not in session:
        return redirect('/login')

    status = request.args.get('status')
    tx_ref = request.args.get('tx_ref')
    transaction_id = request.args.get('transaction_id')

    verification_id = session.get('pending_verification_id')

    if status not in ('successful', 'completed') or not transaction_id:
        return render_template('payment-failed.html')

    if session.get('bettertrust_pending_tx_ref') != tx_ref:
        return render_template('payment-failed.html')

    flw_secret_key = os.getenv('FLUTTERWAVE_SECRET_KEY')

    response = requests.get(
        f'https://api.flutterwave.com/v3/transactions/{transaction_id}/verify',
        headers={"Authorization": f"Bearer {flw_secret_key}"}
    )

    data = response.json()

    if data.get('status') != 'success':
        return render_template('payment-failed.html')

    tx_data = data['data']

    if tx_data['status'] not in ('successful', 'completed'):
        return render_template('payment-failed.html')

    if tx_data['amount'] < 2500:
        return render_template('payment-failed.html')

    if tx_data['currency'] != 'NGN':
        return render_template('payment-failed.html')

    conn = get_db_connection()
    conn.execute('UPDATE verification_requests SET payment_status = ? WHERE id = ?', ('paid', verification_id))
    conn.commit()
    conn.close()

    session.pop('bettertrust_pending_tx_ref', None)

    return redirect('/bettertrust?payment=verified')

@app.route('/bettertrust/initiate-payment', methods=['POST'])
def bettertrust_initiate_payment():
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    verification_id = session.get('pending_verification_id')
    if not verification_id:
        return jsonify({"error": "No verification in progress. Please start over."}), 400

    flw_secret_key = os.getenv('FLUTTERWAVE_SECRET_KEY')
    tx_ref = f"bettertrust-{session['user_id']}-{int(time.time())}"

    session['bettertrust_pending_tx_ref'] = tx_ref

    response = requests.post(
        'https://api.flutterwave.com/v3/payments',
        headers={"Authorization": f"Bearer {flw_secret_key}"},
        json={
            "tx_ref": tx_ref,
            "amount": "2500",
            "currency": "NGN",
            "redirect_url": "http://127.0.0.1:5000/bettertrust/verify-payment",
            "customer": {
                "email": session.get('user_email', 'test@betterwallet.com')
            },
            "customizations": {
                "title": "BetterWallet Better-Trust",
                "description": "Seller verification fee"
            }
        }
    )

    data = response.json()

    if data.get('status') != 'success':
        print("FLUTTERWAVE ERROR:", data)
        return jsonify({"error": "Could not start payment. Please try again."}), 500

    payment_link = data['data']['link']
    return jsonify({"payment_link": payment_link}), 200

@app.route('/bettertrust', methods=['GET', 'POST'])
def bettertrust():
    if 'user_id' not in session:
        return redirect('/login')

    if request.method == 'GET':
        return render_template('bettertrust.html')

    data = request.json
    full_name = data.get('full_name', '').strip()
    business_name = data.get('business_name', '').strip()
    phone = data.get('phone', '').strip()
    email = data.get('email', '').strip()
    address = data.get('address', '').strip()

    if not full_name or not business_name or not phone or not email or not address:
        return jsonify({"error": "All fields are required."}), 400

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO verification_requests (user_id, full_name, business_name, phone, email, address)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (session['user_id'], full_name, business_name, phone, email, address))
    conn.commit()

    new_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    conn.close()

    session['pending_verification_id'] = new_id

    return jsonify({"message": "Details saved.", "verification_id": new_id}), 201

@app.route('/bettertrust/upload-document', methods=['POST'])
def bettertrust_upload_document():
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    verification_id = session.get('pending_verification_id')
    if not verification_id:
        return jsonify({"error": "No verification in progress. Please start over."}), 400

    doc_type = request.form.get('doc_type')
    if doc_type not in ('id_document', 'business_document', 'selfie_document'):
        return jsonify({"error": "Invalid document type."}), 400

    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected."}), 400

    filename = f"verify_{verification_id}_{doc_type}_{file.filename}"
    file.save(os.path.join('static/uploads', filename))

    conn = get_db_connection()
    conn.execute(f'UPDATE verification_requests SET {doc_type} = ? WHERE id = ?', (filename, verification_id))
    conn.commit()
    conn.close()

    return jsonify({"message": "Document uploaded.", "filename": filename}), 200

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()

    new_applications = conn.execute('''
        SELECT COUNT(*) AS count
        FROM applications
        JOIN jobs ON applications.job_id = jobs.id
        WHERE jobs.employer_id = ? AND applications.viewed_by_employer = 0
    ''', (session['user_id'],)).fetchone()['count']

    accepted_applications = conn.execute('''
        SELECT COUNT(*) AS count
        FROM applications
        WHERE applicant_id = ? AND status = 'accepted' AND viewed_by_applicant = 0
    ''', (session['user_id'],)).fetchone()['count']

    user = conn.execute('SELECT profile_photo FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    conn.close()

    staffhook_notifications = new_applications + accepted_applications

    return render_template('dashboard.html', staffhook_notifications=staffhook_notifications, user_photo=user['profile_photo'] if user else None)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return render_template('register.html')

    data = request.json
    name = data.get('name', '').strip()
    email = data.get('email', '').strip().lower()
    phone = data.get('phone', '').strip()
    password = data.get('password', '')

    if not name or not email or not phone or not password:
        return jsonify({"error": "All fields are required."}), 400

    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400

    conn = get_db_connection()
    existing_user = conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()

    if existing_user:
        conn.close()
        return jsonify({"error": "An account with this email already exists."}), 400

    hashed_password = generate_password_hash(password)
    conn.execute(
        'INSERT INTO users (name, email, phone, password) VALUES (?, ?, ?, ?)',
        (name, email, phone, hashed_password)
    )
    conn.commit()
    conn.close()

    return jsonify({"message": "Account created successfully."}), 201

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('login.html')

    data = request.json
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')

    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    conn.close()

    if not user or not check_password_hash(user['password'], password):
        return jsonify({"error": "Invalid email or password."}), 401

    session['user_id'] = user['id']
    session['user_name'] = user['name']
    session['user_email'] = user['email']

    return jsonify({"message": "Login successful."}), 200

@app.route('/staffhook/initiate-payment', methods=['POST'])
def initiate_payment():
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    flw_secret_key = os.getenv('FLUTTERWAVE_SECRET_KEY')
    tx_ref = f"staffhook-{session['user_id']}-{int(time.time())}"

    session['pending_tx_ref'] = tx_ref

    response = requests.post(
        'https://api.flutterwave.com/v3/payments',
        headers={"Authorization": f"Bearer {flw_secret_key}"},
        json={
            "tx_ref": tx_ref,
            "amount": "2500",
            "currency": "NGN",
            "redirect_url": "http://127.0.0.1:5000/staffhook/verify-payment",
            "customer": {
                "email": session.get('user_email', 'test@betterwallet.com')
            },
            "customizations": {
                "title": "BetterWallet StaffHook",
                "description": "Job posting fee"
            }
        }
    )

    data = response.json()

    if data.get('status') != 'success':
        print("FLUTTERWAVE ERROR:", data)
        return jsonify({"error": "Could not start payment. Please try again."}), 500

    payment_link = data['data']['link']
    return jsonify({"payment_link": payment_link}), 200

@app.route('/staffhook/verify-payment')
def verify_payment():
    if 'user_id' not in session:
        return redirect('/login')

    status = request.args.get('status')
    tx_ref = request.args.get('tx_ref')
    transaction_id = request.args.get('transaction_id')

    if status not in ('successful', 'completed') or not transaction_id:
        return render_template('payment-failed.html')

    if session.get('pending_tx_ref') != tx_ref:
        return render_template('payment-failed.html')

    flw_secret_key = os.getenv('FLUTTERWAVE_SECRET_KEY')

    response = requests.get(
        f'https://api.flutterwave.com/v3/transactions/{transaction_id}/verify',
        headers={"Authorization": f"Bearer {flw_secret_key}"}
    )

    data = response.json()

    if data.get('status') != 'success':
        return render_template('payment-failed.html')

    tx_data = data['data']

    if tx_data['status'] not in ('successful', 'completed'):
        return render_template('payment-failed.html')

    if tx_data['amount'] < 2500:
        return render_template('payment-failed.html')

    if tx_data['currency'] != 'NGN':
        return render_template('payment-failed.html')

    session['payment_verified'] = True
    session.pop('pending_tx_ref', None)

    return redirect('/staffhook/post-job?payment=verified')

@app.route('/send-otp', methods=['POST'])
def send_otp():
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    conn = get_db_connection()
    user = conn.execute('SELECT phone FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()

    if not user or not user['phone']:
        return jsonify({"error": "No phone number found on your account."}), 400

    otp_code = str(random.randint(100000, 999999))

    raw_phone = user['phone'].strip()
    if raw_phone.startswith('0'):
        formatted_phone = '234' + raw_phone[1:]
    elif raw_phone.startswith('+234'):
        formatted_phone = raw_phone[1:]
    else:
        formatted_phone = raw_phone

    session['otp_code'] = otp_code
    session['otp_expires_at'] = time.time() + 300  # 5 minutes from now
    session['otp_phone'] = user['phone']

    termii_api_key = os.getenv('TERMII_API_KEY')
    print("TERMII KEY LOADED:", repr(termii_api_key))

    response = requests.post('https://api.ng.termii.com/api/sms/send', json={
        "to": formatted_phone,
        "from": "Termii",
        "sms": f"Your BetterWallet verification code is {otp_code}. It expires in 5 minutes.",
        "type": "plain",
        "channel": "generic",
        "api_key": termii_api_key
    })

    if response.status_code != 200:
        print("TERMII ERROR:", response.status_code, response.text)
        return jsonify({"error": "Failed to send OTP. Please try again."}), 500

    return jsonify({"message": f"OTP sent to {user['phone']}."}), 200

@app.route('/verify-otp', methods=['POST'])
def verify_otp():
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    data = request.json
    entered_code = data.get('otp_code', '').strip()

    stored_code = session.get('otp_code')
    expires_at = session.get('otp_expires_at')

    if not stored_code or not expires_at:
        return jsonify({"error": "No OTP was requested. Please request a new code."}), 400

    if time.time() > expires_at:
        session.pop('otp_code', None)
        session.pop('otp_expires_at', None)
        return jsonify({"error": "OTP has expired. Please request a new code."}), 400

    if entered_code != stored_code:
        return jsonify({"error": "Incorrect code. Please try again."}), 400

    session.pop('otp_code', None)
    session.pop('otp_expires_at', None)
    session['phone_verified'] = True

    return jsonify({"message": "Phone number verified successfully."}), 200

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/me')
def me():
    if 'user_id' in session:
        return jsonify({"logged_in": True, "name": session.get('user_name')})
    return jsonify({"logged_in": False})

@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if 'user_id' not in session:
        return redirect('/login')

    if request.method == 'GET':
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        conn.close()
        return render_template('profile.html', user=user)

    if 'photo' not in request.files:
        return jsonify({"error": "No photo uploaded."}), 400

    photo = request.files['photo']
    if photo.filename == '':
        return jsonify({"error": "No photo selected."}), 400

    filename = f"user_{session['user_id']}_{photo.filename}"
    photo.save(os.path.join('static/uploads', filename))

    conn = get_db_connection()
    conn.execute('UPDATE users SET profile_photo = ? WHERE id = ?', (filename, session['user_id']))
    conn.commit()
    conn.close()

    return jsonify({"message": "Profile photo updated successfully.", "filename": filename}), 200

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message', '')
    reply = get_chat_reply(user_message)
    return jsonify({"reply": reply})

@app.route('/staffhook/post-job', methods=['GET', 'POST'])
def post_job():
    if 'user_id' not in session:
        return redirect('/login')

    if request.method == 'GET':
        return render_template('post-job.html')

    data = request.json
    title = data.get('title', '').strip()
    description = data.get('description', '').strip()
    category = data.get('category', '').strip()
    location = data.get('location', '').strip()
    pay_rate = data.get('pay_rate', '').strip()
    pay_type = data.get('pay_type', '').strip()

    if not title or not description or not category or not location or not pay_rate or not pay_type:
        return jsonify({"error": "All fields are required."}), 400

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO jobs (employer_id, title, description, category, location, pay_rate, pay_type, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
    ''', (session['user_id'], title, description, category, location, pay_rate, pay_type))
    conn.commit()
    conn.close()

    return jsonify({"message": "Job posted successfully and is now live on StaffHook."}), 201

@app.route('/staffhook/jobs')
def find_jobs():
    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()
    jobs = conn.execute('''
        SELECT jobs.*, users.name AS employer_name, users.profile_photo AS employer_photo
        FROM jobs
        LEFT JOIN users ON jobs.employer_id = users.id
        WHERE jobs.status = 'open'
        ORDER BY jobs.created_at DESC
    ''').fetchall()

    new_applications = conn.execute('''
        SELECT COUNT(*) AS count
        FROM applications
        JOIN jobs ON applications.job_id = jobs.id
        WHERE jobs.employer_id = ? AND applications.viewed_by_employer = 0
    ''', (session['user_id'],)).fetchone()['count']

    accepted_applications = conn.execute('''
        SELECT COUNT(*) AS count
        FROM applications
        WHERE applicant_id = ? AND status = 'accepted' AND viewed_by_applicant = 0
    ''', (session['user_id'],)).fetchone()['count']

    conn.close()

    return render_template(
        'find-jobs.html',
        jobs=jobs,
        current_user_id=session['user_id'],
        new_applications=new_applications,
        accepted_applications=accepted_applications
    )

@app.route('/staffhook/apply/<int:job_id>', methods=['POST'])
def apply_to_job(job_id):
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in to apply."}), 401

    data = request.json
    cover_message = data.get('cover_message', '').strip()

    conn = get_db_connection()

    job = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
    if not job:
        conn.close()
        return jsonify({"error": "Job not found."}), 404

    if job['employer_id'] == session['user_id']:
        conn.close()
        return jsonify({"error": "You cannot apply to your own job posting."}), 400

    existing_application = conn.execute(
        'SELECT id FROM applications WHERE job_id = ? AND applicant_id = ?',
        (job_id, session['user_id'])
    ).fetchone()

    if existing_application:
        conn.close()
        return jsonify({"error": "You have already applied to this job."}), 400

    conn.execute('''
        INSERT INTO applications (job_id, applicant_id, cover_message)
        VALUES (?, ?, ?)
    ''', (job_id, session['user_id'], cover_message))
    conn.commit()
    conn.close()

    return jsonify({"message": "Application submitted successfully."}), 201

@app.route('/staffhook/request-job', methods=['GET', 'POST'])
def request_job():
    if 'user_id' not in session:
        return redirect('/login')

    admin_email = os.getenv('ADMIN_EMAIL', '')
    is_admin = session.get('user_email', '').lower() == admin_email.lower()

    if request.method == 'GET':
        conn = get_db_connection()
        workers = conn.execute('''
            SELECT worker_listings.*, users.profile_photo AS user_photo
            FROM worker_listings
            LEFT JOIN users ON worker_listings.user_id = users.id
            WHERE worker_listings.status = 'active'
            ORDER BY worker_listings.is_featured DESC, worker_listings.created_at DESC
        ''').fetchall()
        conn.close()
        return render_template('request-job.html', workers=workers, is_admin=is_admin)

    data = request.json
    full_name = data.get('full_name', '').strip()
    phone = data.get('phone', '').strip()
    skill = data.get('skill', '').strip()
    bio = data.get('bio', '').strip()
    location = data.get('location', '').strip()
    mark_verified = data.get('mark_verified', False)

    if not full_name or not phone or not skill or not location:
        return jsonify({"error": "Name, phone, skill, and location are required."}), 400

    verified_flag = 1 if (is_admin and mark_verified) else 0

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO worker_listings (user_id, full_name, phone, skill, bio, location, status, verified_by_betterwallet)
        VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
    ''', (session['user_id'], full_name, phone, skill, bio, location, verified_flag))
    conn.commit()
    conn.close()

    return jsonify({"message": "Your listing is now live on Request a Job."}), 201

@app.route('/staffhook/upgrade-listing/<int:listing_id>', methods=['POST'])
def upgrade_listing(listing_id):
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    conn = get_db_connection()

    listing = conn.execute('SELECT * FROM worker_listings WHERE id = ?', (listing_id,)).fetchone()

    if not listing:
        conn.close()
        return jsonify({"error": "Listing not found."}), 404

    if listing['user_id'] != session['user_id']:
        conn.close()
        return jsonify({"error": "You can only upgrade your own listing."}), 403

    conn.execute('UPDATE worker_listings SET is_featured = 1 WHERE id = ?', (listing_id,))
    conn.commit()
    conn.close()

    return jsonify({"message": "Your listing is now Featured!"}), 200

@app.route('/staffhook/my-applications')
def my_applications():
    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()
    applications = conn.execute('''
        SELECT applications.id AS application_id,
               applications.status AS application_status,
               applications.cover_message,
               applications.applied_at,
               jobs.title,
               jobs.category,
               jobs.location,
               jobs.pay_rate,
               jobs.pay_type,
               users.name AS employer_name,
               users.phone AS employer_phone,
               users.email AS employer_email
        FROM applications
        JOIN jobs ON applications.job_id = jobs.id
        JOIN users ON jobs.employer_id = users.id
        WHERE applications.applicant_id = ?
        ORDER BY applications.applied_at DESC
    ''', (session['user_id'],)).fetchall()

    conn.execute('''
        UPDATE applications
        SET viewed_by_applicant = 1
        WHERE applicant_id = ? AND status = 'accepted'
    ''', (session['user_id'],))
    conn.commit()
    conn.close()

    return render_template('my-applications.html', applications=applications)

@app.route('/staffhook/my-postings')
def my_postings():
    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()
    jobs = conn.execute('''
        SELECT * FROM jobs WHERE employer_id = ? ORDER BY created_at DESC
    ''', (session['user_id'],)).fetchall()

    jobs_with_applicants = []
    for job in jobs:
        applicants = conn.execute('''
            SELECT applications.id AS application_id,
                   applications.status AS application_status,
                   applications.cover_message,
                   applications.applied_at,
                   users.name AS applicant_name,
                   users.email AS applicant_email
            FROM applications
            JOIN users ON applications.applicant_id = users.id
            WHERE applications.job_id = ?
            ORDER BY applications.applied_at DESC
        ''', (job['id'],)).fetchall()
        jobs_with_applicants.append({'job': job, 'applicants': applicants})

    conn.execute('''
        UPDATE applications
        SET viewed_by_employer = 1
        WHERE job_id IN (SELECT id FROM jobs WHERE employer_id = ?)
    ''', (session['user_id'],))
    conn.commit()
    conn.close()

    return render_template('my-postings.html', jobs_with_applicants=jobs_with_applicants)

@app.route('/staffhook/applications/<int:application_id>/update', methods=['POST'])
def update_application_status(application_id):
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    data = request.json
    new_status = data.get('status', '').strip()

    if new_status not in ('accepted', 'rejected'):
        return jsonify({"error": "Invalid status."}), 400

    conn = get_db_connection()

    application = conn.execute('''
        SELECT applications.id, jobs.employer_id
        FROM applications
        JOIN jobs ON applications.job_id = jobs.id
        WHERE applications.id = ?
    ''', (application_id,)).fetchone()

    if not application:
        conn.close()
        return jsonify({"error": "Application not found."}), 404

    if application['employer_id'] != session['user_id']:
        conn.close()
        return jsonify({"error": "You are not authorized to update this application."}), 403

    conn.execute('UPDATE applications SET status = ? WHERE id = ?', (new_status, application_id))
    conn.commit()
    conn.close()

    return jsonify({"message": f"Application {new_status} successfully."}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
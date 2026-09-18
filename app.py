from flask import Flask, request, jsonify, render_template, session, redirect
from datetime import timedelta
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import os
import random
import time
import requests

load_dotenv()

from chatbot import get_chat_reply

from database import get_db_connection, init_db, init_staffhook_tables, init_bettertrust_tables, init_cac_tables, init_transactions_table

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-this")
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
CORS(app, supports_credentials=True)

def get_serializer():
    return URLSafeTimedSerializer(app.secret_key)

init_db()
init_staffhook_tables()
init_bettertrust_tables()
init_cac_tables()
init_transactions_table()

@app.route('/scuml/admin')
def scuml_admin():
    if 'user_id' not in session:
        return redirect('/login')

    admin_email = os.getenv('ADMIN_EMAIL', '')
    is_admin = session.get('user_email', '').lower() == admin_email.lower()

    if not is_admin:
        return jsonify({"error": "You are not authorized to view this page."}), 403

    conn = get_db_connection()
    registrations = conn.execute('''
        SELECT * FROM cac_registrations
        WHERE payment_status = 'paid' AND registration_type = 'scuml'
        ORDER BY created_at DESC
    ''').fetchall()
    conn.close()

    return render_template('scuml-admin.html', registrations=registrations)

@app.route('/scuml/admin/update/<int:registration_id>', methods=['POST'])
def scuml_admin_update(registration_id):
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
        'UPDATE cac_registrations SET application_status = ? WHERE id = ?',
        (new_status, registration_id)
    )
    conn.commit()
    conn.close()

    return jsonify({"message": f"Registration {new_status}."}), 200

@app.route('/scuml/initiate-payment', methods=['POST'])
def scuml_initiate_payment():
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    registration_id = session.get('pending_scuml_id')
    if not registration_id:
        return jsonify({"error": "No registration in progress. Please start over."}), 400

    flw_secret_key = os.getenv('FLUTTERWAVE_SECRET_KEY')
    tx_ref = f"scuml-{session['user_id']}-{int(time.time())}"

    session['scuml_pending_tx_ref'] = tx_ref

    serializer = get_serializer()
    token = serializer.dumps({
        'user_id': session['user_id'],
        'user_name': session.get('user_name'),
        'user_email': session.get('user_email'),
        'registration_id': registration_id,
        'tx_ref': tx_ref
    })

    response = requests.post(
        'https://api.flutterwave.com/v3/payments',
        headers={"Authorization": f"Bearer {flw_secret_key}"},
        json={
            "tx_ref": tx_ref,
            "amount": "2500",
            "currency": "NGN",
            "redirect_url": f"{os.getenv('BASE_URL', 'http://127.0.0.1:5000')}/scuml/verify-payment/{token}",
            "customer": {
                "email": session.get('user_email', 'test@betterwallet.com')
            },
            "customizations": {
                "title": "BetterWallet SCUML Registration",
                "description": "SCUML registration service fee"
            }
        }
    )

    data = response.json()

    if data.get('status') != 'success':
        print("FLUTTERWAVE ERROR:", data)
        return jsonify({"error": "Could not start payment. Please try again."}), 500

    payment_link = data['data']['link']
    return jsonify({"payment_link": payment_link}), 200

@app.route('/scuml/verify-payment/<token>')
def scuml_verify_payment(token):
    serializer = get_serializer()
    try:
        token_data = serializer.loads(token, max_age=1800)
    except SignatureExpired:
        return render_template('payment-failed.html', retry_url='/cac')
    except BadSignature:
        return render_template('payment-failed.html', retry_url='/cac')

    user_id = token_data['user_id']
    registration_id = token_data['registration_id']
    expected_tx_ref = token_data['tx_ref']

    status = request.args.get('status')
    tx_ref = request.args.get('tx_ref')
    transaction_id = request.args.get('transaction_id')

    if status not in ('successful', 'completed') or not transaction_id:
        return render_template('payment-failed.html', retry_url='/cac')

    if expected_tx_ref != tx_ref:
        return render_template('payment-failed.html', retry_url='/cac')

    flw_secret_key = os.getenv('FLUTTERWAVE_SECRET_KEY')

    response = requests.get(
        f'https://api.flutterwave.com/v3/transactions/{transaction_id}/verify',
        headers={"Authorization": f"Bearer {flw_secret_key}"}
    )

    data = response.json()

    if data.get('status') != 'success':
        return render_template('payment-failed.html', retry_url='/cac')

    tx_data = data['data']

    if tx_data['status'] not in ('successful', 'completed'):
        return render_template('payment-failed.html', retry_url='/cac')

    if tx_data['amount'] < 2500:
        return render_template('payment-failed.html', retry_url='/cac')

    if tx_data['currency'] != 'NGN':
        return render_template('payment-failed.html', retry_url='/cac')

    conn = get_db_connection()
    conn.execute('UPDATE cac_registrations SET payment_status = ? WHERE id = ?', ('paid', registration_id))
    conn.execute('''
        INSERT INTO transactions (user_id, service_type, description, amount, tx_ref, status)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, 'scuml', 'SCUML Registration', tx_data['amount'], tx_ref, 'successful'))
    conn.commit()
    conn.close()

    session.permanent = True
    session['user_id'] = user_id
    if token_data.get('user_name'):
        session['user_name'] = token_data['user_name']
    if token_data.get('user_email'):
        session['user_email'] = token_data['user_email']
    session.pop('scuml_pending_tx_ref', None)

    return redirect('/cac?payment=verified')

@app.route('/scuml', methods=['GET', 'POST'])
def scuml_hub():
    if 'user_id' not in session:
        return redirect('/login')

    if request.method == 'GET':
        return render_template('cac.html')

    data = request.json
    registration_type = data.get('registration_type', '').strip()
    full_name = data.get('full_name', '').strip()
    phone = data.get('phone', '').strip()
    email = data.get('email', '').strip()
    business_name_1 = data.get('business_name_1', '').strip()
    bvn = data.get('bvn', '').strip()
    bank_name = data.get('bank_name', '').strip()
    bank_account_number = data.get('bank_account_number', '').strip()
    tin_number = data.get('tin_number', '').strip()

    if not full_name or not phone or not email or not business_name_1 or not bvn or not bank_name or not bank_account_number or not tin_number:
        return jsonify({"success": False, "message": "All required fields must be filled."}), 400

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO cac_registrations (
            user_id, registration_type, full_name, phone, email,
            business_name_1, bvn, bank_name, bank_account_number, tin_number
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        session['user_id'], registration_type, full_name, phone, email,
        business_name_1, bvn, bank_name, bank_account_number, tin_number
    ))
    conn.commit()

    new_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    conn.close()

    session['pending_scuml_id'] = new_id

    return jsonify({"success": True, "registration_id": new_id}), 201

@app.route('/my-transactions')
def my_transactions():
    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()
    transactions = conn.execute('''
        SELECT * FROM transactions
        WHERE user_id = ?
        ORDER BY created_at DESC
    ''', (session['user_id'],)).fetchall()
    conn.close()

    return render_template('my-transactions.html', transactions=transactions)

@app.route('/my-transactions-data')
def my_transactions_data():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in."}), 401

    conn = get_db_connection()
    transactions = conn.execute('''
        SELECT * FROM transactions
        WHERE user_id = ?
        ORDER BY created_at DESC
    ''', (session['user_id'],)).fetchall()
    conn.close()

    transactions_list = []
    for tx in transactions:
        transactions_list.append({
            "id": tx["id"],
            "service_type": tx["service_type"],
            "description": tx["description"],
            "amount": tx["amount"],
            "status": tx["status"],
            "created_at": tx["created_at"]
        })

    return jsonify({"transactions": transactions_list}), 200

@app.route('/scuml/upload-document', methods=['POST'])
def scuml_upload_document():
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    registration_id = session.get('pending_scuml_id')
    if not registration_id:
        return jsonify({"error": "No registration in progress. Please start over."}), 400

    doc_type = request.form.get('doc_type')
    if doc_type not in ('id_document', 'passport_photo'):
        return jsonify({"error": "Invalid document type."}), 400

    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected."}), 400

    filename = f"scuml_{registration_id}_{doc_type}_{file.filename}"
    file.save(os.path.join('static/uploads', filename))

    conn = get_db_connection()
    conn.execute(f'UPDATE cac_registrations SET {doc_type} = ? WHERE id = ?', (filename, registration_id))
    conn.commit()
    conn.close()

    return jsonify({"message": "Document uploaded.", "filename": filename}), 200

@app.route('/trademark/admin')
def trademark_admin():
    if 'user_id' not in session:
        return redirect('/login')

    admin_email = os.getenv('ADMIN_EMAIL', '')
    is_admin = session.get('user_email', '').lower() == admin_email.lower()

    if not is_admin:
        return jsonify({"error": "You are not authorized to view this page."}), 403

    conn = get_db_connection()
    registrations = conn.execute('''
        SELECT * FROM cac_registrations
        WHERE payment_status = 'paid' AND registration_type = 'trademark'
        ORDER BY created_at DESC
    ''').fetchall()
    conn.close()

    return render_template('trademark-admin.html', registrations=registrations)

@app.route('/trademark/admin/update/<int:registration_id>', methods=['POST'])
def trademark_admin_update(registration_id):
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
        'UPDATE cac_registrations SET application_status = ? WHERE id = ?',
        (new_status, registration_id)
    )
    conn.commit()
    conn.close()

    return jsonify({"message": f"Registration {new_status}."}), 200

@app.route('/trademark/initiate-payment', methods=['POST'])
def trademark_initiate_payment():
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    registration_id = session.get('pending_trademark_id')
    if not registration_id:
        return jsonify({"error": "No registration in progress. Please start over."}), 400

    flw_secret_key = os.getenv('FLUTTERWAVE_SECRET_KEY')
    tx_ref = f"trademark-{session['user_id']}-{int(time.time())}"

    session['trademark_pending_tx_ref'] = tx_ref

    serializer = get_serializer()
    token = serializer.dumps({
        'user_id': session['user_id'],
        'user_name': session.get('user_name'),
        'user_email': session.get('user_email'),
        'registration_id': registration_id,
        'tx_ref': tx_ref
    })

    response = requests.post(
        'https://api.flutterwave.com/v3/payments',
        headers={"Authorization": f"Bearer {flw_secret_key}"},
        json={
            "tx_ref": tx_ref,
            "amount": "2500",
            "currency": "NGN",
            "redirect_url": f"{os.getenv('BASE_URL', 'http://127.0.0.1:5000')}/trademark/verify-payment/{token}",
            "customer": {
                "email": session.get('user_email', 'test@betterwallet.com')
            },
            "customizations": {
                "title": "BetterWallet Trademark Registration",
                "description": "Trademark registration fee"
            }
        }
    )

    data = response.json()

    if data.get('status') != 'success':
        print("FLUTTERWAVE ERROR:", data)
        return jsonify({"error": "Could not start payment. Please try again."}), 500

    payment_link = data['data']['link']
    return jsonify({"payment_link": payment_link}), 200

@app.route('/trademark/upload-document', methods=['POST'])
def trademark_upload_document():
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    registration_id = session.get('pending_trademark_id')
    if not registration_id:
        return jsonify({"error": "No registration in progress. Please start over."}), 400

    doc_type = request.form.get('doc_type')
    if doc_type not in ('id_document', 'passport_photo', 'mark_logo_document'):
        return jsonify({"error": "Invalid document type."}), 400

    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected."}), 400

    filename = f"trademark_{registration_id}_{doc_type}_{file.filename}"
    file.save(os.path.join('static/uploads', filename))

    conn = get_db_connection()
    conn.execute(f'UPDATE cac_registrations SET {doc_type} = ? WHERE id = ?', (filename, registration_id))
    conn.commit()
    conn.close()

    return jsonify({"message": "Document uploaded.", "filename": filename}), 200

@app.route('/trademark', methods=['GET', 'POST'])
def trademark_hub():
    if 'user_id' not in session:
        return redirect('/login')

    if request.method == 'GET':
        return render_template('cac.html')

    data = request.json
    registration_type = data.get('registration_type', '').strip()
    full_name = data.get('full_name', '').strip()
    phone = data.get('phone', '').strip()
    email = data.get('email', '').strip()
    business_name_1 = data.get('business_name_1', '').strip()
    trademark_class = data.get('trademark_class', '').strip()
    trademark_type = data.get('trademark_type', '').strip()
    goods_services_description = data.get('goods_services_description', '').strip()

    if not full_name or not phone or not email or not business_name_1 or not trademark_class or not trademark_type or not goods_services_description:
        return jsonify({"success": False, "message": "All required fields must be filled."}), 400

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO cac_registrations (
            user_id, registration_type, full_name, phone, email,
            business_name_1, trademark_class, trademark_type, goods_services_description
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        session['user_id'], registration_type, full_name, phone, email,
        business_name_1, trademark_class, trademark_type, goods_services_description
    ))
    conn.commit()

    new_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    conn.close()

    session['pending_trademark_id'] = new_id

    return jsonify({"success": True, "registration_id": new_id}), 201

@app.route('/cac/my-registrations')
def cac_my_registrations():
    if 'user_id' not in session:
        return redirect('/login')

    conn = get_db_connection()
    registrations = conn.execute('''
        SELECT * FROM cac_registrations
        WHERE user_id = ?
        ORDER BY created_at DESC
    ''', (session['user_id'],)).fetchall()
    conn.close()

    return render_template('my-cac-registrations.html', registrations=registrations)

@app.route('/cac/my-registrations-data')
def cac_my_registrations_data():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in."}), 401

    conn = get_db_connection()
    registrations = conn.execute('''
        SELECT * FROM cac_registrations
        WHERE user_id = ?
        ORDER BY created_at DESC
    ''', (session['user_id'],)).fetchall()
    conn.close()

    registrations_list = []
    for reg in registrations:
        registrations_list.append({
            "id": reg["id"],
            "registration_type": reg["registration_type"],
            "business_name_1": reg["business_name_1"],
            "payment_status": reg["payment_status"],
            "application_status": reg["application_status"],
            "created_at": reg["created_at"]
        })

    return jsonify({"registrations": registrations_list}), 200

@app.route('/cac/registration/<int:registration_id>')
def cac_registration_detail(registration_id):
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in."}), 401

    conn = get_db_connection()
    reg = conn.execute('''
        SELECT * FROM cac_registrations
        WHERE id = ? AND user_id = ?
    ''', (registration_id, session['user_id'])).fetchone()
    conn.close()

    if not reg:
        return jsonify({"error": "Registration not found."}), 404

    base_url = os.getenv('BASE_URL', 'http://127.0.0.1:5000')

    return jsonify({
        "id": reg["id"],
        "registration_type": reg["registration_type"],
        "full_name": reg["full_name"],
        "phone": reg["phone"],
        "email": reg["email"],
        "business_name_1": reg["business_name_1"],
        "business_name_2": reg["business_name_2"],
        "nature_of_business": reg["nature_of_business"],
        "business_address": reg["business_address"],
        "bvn": reg["bvn"],
        "bank_name": reg["bank_name"],
        "bank_account_number": reg["bank_account_number"],
        "tin_number": reg["tin_number"],
        "trademark_class": reg["trademark_class"],
        "trademark_type": reg["trademark_type"],
        "goods_services_description": reg["goods_services_description"],
        "payment_status": reg["payment_status"],
        "application_status": reg["application_status"],
        "created_at": reg["created_at"],
        "id_document_url": f"{base_url}/static/uploads/{reg['id_document']}" if reg["id_document"] else None,
        "passport_photo_url": f"{base_url}/static/uploads/{reg['passport_photo']}" if reg["passport_photo"] else None
    }), 200

@app.route('/cac/verify-payment/<token>')
def cac_verify_payment(token):
    serializer = get_serializer()
    try:
        token_data = serializer.loads(token, max_age=1800)  # 30 minutes
    except SignatureExpired:
        return render_template('payment-failed.html', retry_url='/cac')
    except BadSignature:
        return render_template('payment-failed.html', retry_url='/cac')

    user_id = token_data['user_id']
    registration_id = token_data['registration_id']
    expected_tx_ref = token_data['tx_ref']

    status = request.args.get('status')
    tx_ref = request.args.get('tx_ref')
    transaction_id = request.args.get('transaction_id')

    if status not in ('successful', 'completed') or not transaction_id:
        return render_template('payment-failed.html', retry_url='/cac')

    if expected_tx_ref != tx_ref:
        return render_template('payment-failed.html', retry_url='/cac')

    flw_secret_key = os.getenv('FLUTTERWAVE_SECRET_KEY')

    response = requests.get(
        f'https://api.flutterwave.com/v3/transactions/{transaction_id}/verify',
        headers={"Authorization": f"Bearer {flw_secret_key}"}
    )

    data = response.json()

    if data.get('status') != 'success':
        return render_template('payment-failed.html', retry_url='/cac')

    tx_data = data['data']

    if tx_data['status'] not in ('successful', 'completed'):
        return render_template('payment-failed.html', retry_url='/cac')

    if tx_data['amount'] < 2500:
        return render_template('payment-failed.html', retry_url='/cac')

    if tx_data['currency'] != 'NGN':
        return render_template('payment-failed.html', retry_url='/cac')

    conn = get_db_connection()
    conn.execute('UPDATE cac_registrations SET payment_status = ? WHERE id = ?', ('paid', registration_id))
    conn.execute('''
        INSERT INTO transactions (user_id, service_type, description, amount, tx_ref, status)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, 'cac', 'CAC Registration', tx_data['amount'], tx_ref, 'successful'))
    conn.commit()
    conn.close()

    # Re-establish the session from the token in case the WebView cookie was never set
    session.permanent = True
    session['user_id'] = user_id
    if token_data.get('user_name'):
        session['user_name'] = token_data['user_name']
    if token_data.get('user_email'):
        session['user_email'] = token_data['user_email']
    session.pop('cac_pending_tx_ref', None)

    return redirect('/cac?payment=verified')

@app.route('/trademark/verify-payment/<token>')
def trademark_verify_payment(token):
    serializer = get_serializer()
    try:
        token_data = serializer.loads(token, max_age=1800)
    except SignatureExpired:
        return render_template('payment-failed.html', retry_url='/cac')
    except BadSignature:
        return render_template('payment-failed.html', retry_url='/cac')

    user_id = token_data['user_id']
    registration_id = token_data['registration_id']
    expected_tx_ref = token_data['tx_ref']

    status = request.args.get('status')
    tx_ref = request.args.get('tx_ref')
    transaction_id = request.args.get('transaction_id')

    if status not in ('successful', 'completed') or not transaction_id:
        return render_template('payment-failed.html', retry_url='/cac')

    if expected_tx_ref != tx_ref:
        return render_template('payment-failed.html', retry_url='/cac')

    flw_secret_key = os.getenv('FLUTTERWAVE_SECRET_KEY')

    response = requests.get(
        f'https://api.flutterwave.com/v3/transactions/{transaction_id}/verify',
        headers={"Authorization": f"Bearer {flw_secret_key}"}
    )

    data = response.json()

    if data.get('status') != 'success':
        return render_template('payment-failed.html', retry_url='/cac')

    tx_data = data['data']

    if tx_data['status'] not in ('successful', 'completed'):
        return render_template('payment-failed.html', retry_url='/cac')

    if tx_data['amount'] < 2500:
        return render_template('payment-failed.html', retry_url='/cac')

    if tx_data['currency'] != 'NGN':
        return render_template('payment-failed.html', retry_url='/cac')

    conn = get_db_connection()
    conn.execute('UPDATE cac_registrations SET payment_status = ? WHERE id = ?', ('paid', registration_id))
    conn.execute('''
        INSERT INTO transactions (user_id, service_type, description, amount, tx_ref, status)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, 'trademark', 'Trademark Registration', tx_data['amount'], tx_ref, 'successful'))
    conn.commit()
    conn.close()

    session.permanent = True
    session['user_id'] = user_id
    if token_data.get('user_name'):
        session['user_name'] = token_data['user_name']
    if token_data.get('user_email'):
        session['user_email'] = token_data['user_email']
    session.pop('trademark_pending_tx_ref', None)

    return redirect('/cac?payment=verified')

@app.route('/cac/admin/upload-certificate/<int:registration_id>', methods=['POST'])
def cac_admin_upload_certificate(registration_id):
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    admin_email = os.getenv('ADMIN_EMAIL', '')
    is_admin = session.get('user_email', '').lower() == admin_email.lower()

    if not is_admin:
        return jsonify({"error": "You are not authorized to do this."}), 403

    doc_type = request.form.get('doc_type')
    if doc_type not in ('cac_certificate_document', 'cac_status_report'):
        return jsonify({"error": "Invalid document type."}), 400

    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected."}), 400

    filename = f"cac_{registration_id}_{doc_type}_{file.filename}"
    file.save(os.path.join('static/uploads', filename))

    conn = get_db_connection()
    conn.execute(f'UPDATE cac_registrations SET {doc_type} = ? WHERE id = ?', (filename, registration_id))
    conn.commit()
    conn.close()

    return jsonify({"message": "Document uploaded.", "filename": filename}), 200

@app.route('/cac/admin')
def cac_admin():
    if 'user_id' not in session:
        return redirect('/login')

    admin_email = os.getenv('ADMIN_EMAIL', '')
    is_admin = session.get('user_email', '').lower() == admin_email.lower()

    if not is_admin:
        return jsonify({"error": "You are not authorized to view this page."}), 403

    conn = get_db_connection()
    registrations = conn.execute('''
        SELECT * FROM cac_registrations
        WHERE payment_status = 'paid'
        ORDER BY created_at DESC
    ''').fetchall()
    conn.close()

    return render_template('cac-admin.html', registrations=registrations)

@app.route('/cac/initiate-payment', methods=['POST'])
def cac_initiate_payment():
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401
    registration_id = session.get('pending_cac_id')
    if not registration_id:
        return jsonify({"error": "No registration in progress. Please start over."}), 400

    flw_secret_key = os.getenv('FLUTTERWAVE_SECRET_KEY')
    tx_ref = f"cac-{session['user_id']}-{int(time.time())}"

    session['cac_pending_tx_ref'] = tx_ref

    serializer = get_serializer()
    token = serializer.dumps({
        'user_id': session['user_id'],
        'user_name': session.get('user_name'),
        'user_email': session.get('user_email'),
        'registration_id': registration_id,
        'tx_ref': tx_ref
    })

    response = requests.post(
        'https://api.flutterwave.com/v3/payments',
        headers={"Authorization": f"Bearer {flw_secret_key}"},
        json={
            "tx_ref": tx_ref,
            "amount": "2500",
            "currency": "NGN",
            "redirect_url": f"{os.getenv('BASE_URL', 'http://127.0.0.1:5000')}/cac/verify-payment/{token}",
            "customer": {
                "email": session.get('user_email', 'test@betterwallet.com')
            },
            "customizations": {
                "title": "BetterWallet CAC Registration",
                "description": "CAC business registration fee"
            }
        }
    )

    data = response.json()

    if data.get('status') != 'success':
        print("FLUTTERWAVE ERROR:", data)
        return jsonify({"error": "Could not start payment. Please try again."}), 500

    payment_link = data['data']['link']
    return jsonify({"payment_link": payment_link}), 200

@app.route('/cac/upload-document', methods=['POST'])
def cac_upload_document():
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    registration_id = session.get('pending_cac_id')
    if not registration_id:
        return jsonify({"error": "No registration in progress. Please start over."}), 400

    doc_type = request.form.get('doc_type')
    if doc_type not in ('id_document', 'passport_photo'):
        return jsonify({"error": "Invalid document type."}), 400

    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected."}), 400

    filename = f"cac_{registration_id}_{doc_type}_{file.filename}"
    file.save(os.path.join('static/uploads', filename))

    conn = get_db_connection()
    conn.execute(f'UPDATE cac_registrations SET {doc_type} = ? WHERE id = ?', (filename, registration_id))
    conn.commit()
    conn.close()

    return jsonify({"message": "Document uploaded.", "filename": filename}), 200


@app.route('/cac', methods=['GET', 'POST'])
def cac_hub():
    if 'user_id' not in session:
        return redirect('/login')

    if request.method == 'GET':
        return render_template('cac.html')

    data = request.json
    registration_type = data.get('registration_type', '').strip()
    full_name = data.get('full_name', '').strip()
    phone = data.get('phone', '').strip()
    email = data.get('email', '').strip()
    business_name_1 = data.get('business_name_1', '').strip()
    business_name_2 = data.get('business_name_2', '').strip()
    nature_of_business = data.get('nature_of_business', '').strip()
    business_address = data.get('business_address', '').strip()

    if not full_name or not phone or not email or not business_name_1 or not nature_of_business or not business_address:
        return jsonify({"success": False, "message": "All required fields must be filled."}), 400

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO cac_registrations (
            user_id, registration_type, full_name, phone, email,
            business_name_1, business_name_2, nature_of_business, business_address
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        session['user_id'], registration_type, full_name, phone, email,
        business_name_1, business_name_2, nature_of_business, business_address
    ))
    conn.commit()

    new_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    conn.close()

    session['pending_cac_id'] = new_id

    return jsonify({"success": True, "registration_id": new_id}), 201

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

@app.route('/bettertrust/verify-payment/<token>')
def bettertrust_verify_payment(token):
    serializer = get_serializer()
    try:
        token_data = serializer.loads(token, max_age=1800)
    except SignatureExpired:
        return render_template('payment-failed.html', retry_url='/bettertrust')
    except BadSignature:
        return render_template('payment-failed.html', retry_url='/bettertrust')

    user_id = token_data['user_id']
    verification_id = token_data['verification_id']
    expected_tx_ref = token_data['tx_ref']

    status = request.args.get('status')
    tx_ref = request.args.get('tx_ref')
    transaction_id = request.args.get('transaction_id')

    if status not in ('successful', 'completed') or not transaction_id:
        return render_template('payment-failed.html', retry_url='/bettertrust')

    if expected_tx_ref != tx_ref:
        return render_template('payment-failed.html', retry_url='/bettertrust')

    flw_secret_key = os.getenv('FLUTTERWAVE_SECRET_KEY')

    response = requests.get(
        f'https://api.flutterwave.com/v3/transactions/{transaction_id}/verify',
        headers={"Authorization": f"Bearer {flw_secret_key}"}
    )

    data = response.json()

    if data.get('status') != 'success':
        return render_template('payment-failed.html', retry_url='/bettertrust')

    tx_data = data['data']

    if tx_data['status'] not in ('successful', 'completed'):
        return render_template('payment-failed.html', retry_url='/bettertrust')

    if tx_data['amount'] < 2500:
        return render_template('payment-failed.html', retry_url='/bettertrust')

    if tx_data['currency'] != 'NGN':
        return render_template('payment-failed.html', retry_url='/bettertrust')

    conn = get_db_connection()
    conn.execute('UPDATE verification_requests SET payment_status = ? WHERE id = ?', ('paid', verification_id))
    conn.execute('''
        INSERT INTO transactions (user_id, service_type, description, amount, tx_ref, status)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, 'bettertrust', 'Better-Trust Verification', tx_data['amount'], tx_ref, 'successful'))
    conn.commit()
    conn.close()

    session.permanent = True
    session['user_id'] = user_id
    if token_data.get('user_name'):
        session['user_name'] = token_data['user_name']
    if token_data.get('user_email'):
        session['user_email'] = token_data['user_email']
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

    serializer = get_serializer()
    token = serializer.dumps({
        'user_id': session['user_id'],
        'user_name': session.get('user_name'),
        'user_email': session.get('user_email'),
        'verification_id': verification_id,
        'tx_ref': tx_ref
    })

    response = requests.post(
        'https://api.flutterwave.com/v3/payments',
        headers={"Authorization": f"Bearer {flw_secret_key}"},
        json={
            "tx_ref": tx_ref,
            "amount": "2500",
            "currency": "NGN",
            "redirect_url": f"{os.getenv('BASE_URL', 'http://127.0.0.1:5000')}/bettertrust/verify-payment/{token}",
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

@app.route('/staffhook/request-job/upload-photo', methods=['POST'])
def request_job_upload_photo():
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    listing_id = session.get('pending_worker_listing_id')
    if not listing_id:
        return jsonify({"error": "No listing in progress. Please start over."}), 400

    photo_number = request.form.get('photo_number')
    if photo_number not in ('1', '2'):
        return jsonify({"error": "Invalid photo number."}), 400

    column = 'photo' if photo_number == '1' else 'photo_2'

    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected."}), 400

    filename = f"worker_{listing_id}_photo{photo_number}_{file.filename}"
    file.save(os.path.join('static/uploads', filename))

    conn = get_db_connection()
    conn.execute(f'UPDATE worker_listings SET {column} = ? WHERE id = ?', (filename, listing_id))
    conn.commit()
    conn.close()

    return jsonify({"message": "Photo uploaded.", "filename": filename}), 200

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/dashboard-data')
def dashboard_data():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in."}), 401

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

    recent_transactions = conn.execute('''
        SELECT * FROM transactions
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 4
    ''', (session['user_id'],)).fetchall()

    conn.close()

    staffhook_notifications = new_applications + accepted_applications

    transactions_list = []
    for tx in recent_transactions:
        transactions_list.append({
            "id": tx["id"],
            "service_type": tx["service_type"],
            "description": tx["description"],
            "amount": tx["amount"],
            "status": tx["status"],
            "created_at": tx["created_at"]
        })

    return jsonify({
        "balance": 248500.00,
        "user_name": session.get('user_name'),
        "user_photo": user['profile_photo'] if user else None,
        "staffhook_notifications": staffhook_notifications,
        "recent_transactions": transactions_list
    }), 200

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect('/login?reason=timeout')

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

    recent_transactions = conn.execute('''
        SELECT * FROM transactions
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 4
    ''', (session['user_id'],)).fetchall()

    conn.close()

    staffhook_notifications = new_applications + accepted_applications

    return render_template('dashboard.html', staffhook_notifications=staffhook_notifications, user_photo=user['profile_photo'] if user else None, recent_transactions=recent_transactions)

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

    session.permanent = True
    session['user_id'] = user['id']
    session['user_name'] = user['name']
    session['user_email'] = user['email']

    return jsonify({"message": "Login successful."}), 200

@app.route('/staffhook/initiate-payment', methods=['POST'])
def initiate_payment():
    if 'user_id' not in session:
        return jsonify({"error": "You must be logged in."}), 401

    data = request.json or {}

    flw_secret_key = os.getenv('FLUTTERWAVE_SECRET_KEY')
    tx_ref = f"staffhook-{session['user_id']}-{int(time.time())}"

    session['pending_tx_ref'] = tx_ref

    serializer = get_serializer()
    token = serializer.dumps({
        'user_id': session['user_id'],
        'user_name': session.get('user_name'),
        'user_email': session.get('user_email'),
        'tx_ref': tx_ref,
        'title': data.get('title', '').strip(),
        'description': data.get('description', '').strip(),
        'category': data.get('category', '').strip(),
        'location': data.get('location', '').strip(),
        'pay_rate': data.get('pay_rate', '').strip(),
        'pay_type': data.get('pay_type', '').strip()
    })

    response = requests.post(
        'https://api.flutterwave.com/v3/payments',
        headers={"Authorization": f"Bearer {flw_secret_key}"},
        json={
            "tx_ref": tx_ref,
            "amount": "2500",
            "currency": "NGN",
            "redirect_url": f"{os.getenv('BASE_URL', 'http://127.0.0.1:5000')}/staffhook/verify-payment/{token}",
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

@app.route('/staffhook/verify-payment/<token>')
def verify_payment(token):
    serializer = get_serializer()
    try:
        token_data = serializer.loads(token, max_age=1800)
    except SignatureExpired:
        return render_template('payment-failed.html', retry_url='/staffhook/post-job')
    except BadSignature:
        return render_template('payment-failed.html', retry_url='/staffhook/post-job')

    user_id = token_data['user_id']
    expected_tx_ref = token_data['tx_ref']

    status = request.args.get('status')
    tx_ref = request.args.get('tx_ref')
    transaction_id = request.args.get('transaction_id')

    if status not in ('successful', 'completed') or not transaction_id:
        return render_template('payment-failed.html', retry_url='/staffhook/post-job')

    if expected_tx_ref != tx_ref:
        return render_template('payment-failed.html', retry_url='/staffhook/post-job')

    flw_secret_key = os.getenv('FLUTTERWAVE_SECRET_KEY')

    response = requests.get(
        f'https://api.flutterwave.com/v3/transactions/{transaction_id}/verify',
        headers={"Authorization": f"Bearer {flw_secret_key}"}
    )

    data = response.json()

    if data.get('status') != 'success':
        return render_template('payment-failed.html', retry_url='/staffhook/post-job')

    tx_data = data['data']

    if tx_data['status'] not in ('successful', 'completed'):
        return render_template('payment-failed.html', retry_url='/staffhook/post-job')

    if tx_data['amount'] < 2500:
        return render_template('payment-failed.html', retry_url='/staffhook/post-job')

    if tx_data['currency'] != 'NGN':
        return render_template('payment-failed.html', retry_url='/staffhook/post-job')

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO transactions (user_id, service_type, description, amount, tx_ref, status)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, 'staffhook', 'StaffHook Job Posting', tx_data['amount'], tx_ref, 'successful'))
    conn.commit()
    conn.close()

    session.permanent = True
    session['user_id'] = user_id
    if token_data.get('user_name'):
        session['user_name'] = token_data['user_name']
    if token_data.get('user_email'):
        session['user_email'] = token_data['user_email']
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

@app.route('/profile-data')
def profile_data():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in."}), 401

    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()

    if not user:
        return jsonify({"error": "User not found."}), 404

    base_url = os.getenv('BASE_URL', 'http://127.0.0.1:5000')

    return jsonify({
        "name": user["name"],
        "email": user["email"],
        "phone": user["phone"],
        "profile_photo_url": f"{base_url}/static/uploads/{user['profile_photo']}" if user["profile_photo"] else None
    }), 200


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

    if not session.get('payment_verified'):
        return jsonify({"error": "Please complete payment before posting a job."}), 402

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

@app.route('/staffhook/jobs-data')
def find_jobs_data():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in."}), 401

    conn = get_db_connection()
    jobs = conn.execute('''
        SELECT jobs.*, users.name AS employer_name
        FROM jobs
        LEFT JOIN users ON jobs.employer_id = users.id
        WHERE jobs.status = 'open'
        ORDER BY jobs.created_at DESC
    ''').fetchall()
    conn.close()

    jobs_list = []
    for job in jobs:
        jobs_list.append({
            "id": job["id"],
            "title": job["title"],
            "description": job["description"],
            "category": job["category"],
            "location": job["location"],
            "pay_rate": job["pay_rate"],
            "pay_type": job["pay_type"],
            "employer_name": job["employer_name"],
            "created_at": job["created_at"]
        })

    return jsonify({"jobs": jobs_list}), 200

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

    new_id = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    conn.close()

    session['pending_worker_listing_id'] = new_id

    return jsonify({"message": "Your listing is now live on Request a Job.", "listing_id": new_id}), 201

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
from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
import os

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "attendance.db")
app.secret_key = "attendance_tracker_secret"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def create_database():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            register_no TEXT NOT NULL UNIQUE,
            department TEXT NOT NULL,
            section TEXT NOT NULL,
            password TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            attendance_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('Present','Absent')),
            UNIQUE(student_id, attendance_date),
            FOREIGN KEY(student_id) REFERENCES students(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_account (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL
        )
    """)
    cur.execute(
        "INSERT OR IGNORE INTO admin_account(id, username, password) VALUES (1, 'admin', '1234')"
    )

    conn.commit()
    conn.close()


def normalize_existing_attendance():
    """Convert old YYYY-MM-DD FN/AN rows to one daily record per student."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(attendance)")
    cols = [row["name"] for row in cur.fetchall()]
    if not cols:
        conn.close()
        return

    rows = cur.execute("""
        SELECT student_id, attendance_date, status
        FROM attendance
        ORDER BY id
    """).fetchall()

    # Existing database may have duplicate dates (FN/AN).
    grouped = {}
    for row in rows:
        raw_date = (row["attendance_date"] or "").strip()
        date_only = raw_date[:10]  # intentionally ignores FN/AN
        if len(date_only) != 10:
            continue
        key = (row["student_id"], date_only)

        # If old FN/AN entries disagree, keep Present if at least one session
        # was Present; otherwise keep Absent. FN/AN itself is never stored.
        old_status = row["status"]
        if key not in grouped:
            grouped[key] = old_status
        elif old_status == "Present":
            grouped[key] = "Present"

    cur.execute("DELETE FROM attendance")
    for (student_id, date_only), status in grouped.items():
        if status not in ("Present", "Absent"):
            status = "Absent"
        cur.execute("""
            INSERT OR REPLACE INTO attendance(student_id, attendance_date, status)
            VALUES (?, ?, ?)
        """, (student_id, date_only, status))

    conn.commit()
    conn.close()


def setup_database():
    # First create compatible tables. Existing DB is retained.
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            register_no TEXT NOT NULL,
            department TEXT NOT NULL,
            section TEXT NOT NULL,
            password TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER,
            attendance_date TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_account (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL
        )
    """)
    cur.execute(
        "INSERT OR IGNORE INTO admin_account(id, username, password) VALUES (1, 'admin', '1234')"
    )
    conn.commit()
    conn.close()

    normalize_existing_attendance()

    # Add unique index for daily attendance, even on an old DB schema.
    conn = get_db()
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_student_date
        ON attendance(student_id, attendance_date)
    """)
    conn.commit()
    conn.close()


# ---------------- HOME / LOGIN ----------------

@app.route("/")
def home():
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    conn = get_db()
    admin = conn.execute(
        "SELECT username FROM admin_account WHERE username=? AND password=?",
        (username, password)
    ).fetchone()
    conn.close()

    if admin:
        session.clear()
        session["admin"] = True
        session["admin_username"] = admin["username"]
        return redirect(url_for("dashboard"))

    return "Invalid Username or Password"


@app.route("/student_login", methods=["POST"])
def student_login():
    register_no = request.form.get("register_no", "").strip()
    password = request.form.get("password", "")

    conn = get_db()
    student = conn.execute("""
        SELECT id, name, register_no, department, section
        FROM students
        WHERE register_no = ? AND password = ?
    """, (register_no, password)).fetchone()
    conn.close()

    if student:
        session.clear()
        session["student_id"] = student["id"]
        session["student_name"] = student["name"]
        session["student_register_no"] = student["register_no"]
        session["student_department"] = student["department"]
        session["student_section"] = student["section"]
        return redirect(url_for("student_dashboard"))

    return "Invalid Register Number or Password"


# ---------------- FORGOT PASSWORD ----------------

@app.route("/forgot_password")
def forgot_password():
    return render_template("forgot_password.html")


@app.route("/reset_password", methods=["POST"])
def reset_password():
    role = request.form.get("role")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not new_password or new_password != confirm_password:
        return "New password and confirm password do not match."

    conn = get_db()
    cur = conn.cursor()

    if role == "admin":
        username = request.form.get("username", "").strip()
        cur.execute(
            "UPDATE admin_account SET password=? WHERE username=?",
            (new_password, username)
        )
    elif role == "student":
        register_no = request.form.get("register_no", "").strip()
        name = request.form.get("name", "").strip()
        cur.execute(
            "UPDATE students SET password=? WHERE register_no=? AND name=?",
            (new_password, register_no, name)
        )
    else:
        conn.close()
        return "Invalid reset option."

    changed = cur.rowcount
    conn.commit()
    conn.close()

    if changed:
        return redirect(url_for("home"))
    return "The details entered for password reset are not valid."


# ---------------- STUDENT ----------------

@app.route("/student_dashboard")
def student_dashboard():
    if "student_id" not in session:
        return redirect(url_for("home"))

    student_id = session["student_id"]
    conn = get_db()
    result = conn.execute("""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN status='Present' THEN 1 ELSE 0 END),0) AS present
        FROM attendance
        WHERE student_id = ?
    """, (student_id,)).fetchone()
    conn.close()

    total = result["total"]
    present = result["present"]
    percentage = round((present / total) * 100, 2) if total else 0

    student = (
        session["student_name"],
        session["student_register_no"],
        session["student_department"],
        session["student_section"]
    )
    return render_template("student_dashboard.html", student=student, percentage=percentage)


@app.route("/student_settings")
def student_settings():
    if "student_id" not in session:
        return redirect(url_for("home"))
    return render_template("student_settings.html", student_name=session["student_name"])


@app.route("/student_logout")
def student_logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/my_attendance")
def my_attendance():
    if "student_id" not in session:
        return redirect(url_for("home"))

    selected_date = request.args.get("date")
    conn = get_db()
    records = conn.execute("""
        SELECT attendance_date, status
        FROM attendance
        WHERE student_id=?
        ORDER BY attendance_date
    """, (session["student_id"],)).fetchall()
    conn.close()

    attendance_records = {row["attendance_date"]: row["status"] for row in records}
    status = attendance_records.get(selected_date) if selected_date else None

    return render_template(
        "my_attendance.html",
        student_name=session["student_name"],
        selected_date=selected_date,
        status=status,
        attendance_records=attendance_records
    )


# ---------------- ADMIN ----------------

@app.route("/dashboard")
def dashboard():
    if not session.get("admin"):
        return redirect(url_for("home"))
    return render_template("dashboard.html")


@app.route("/settings")
def settings():
    if not session.get("admin"):
        return redirect(url_for("home"))
    return render_template("settings.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ---------------- STUDENTS ----------------

@app.route("/students")
def students():
    if not session.get("admin"):
        return redirect(url_for("home"))

    conn = get_db()
    rows = conn.execute("""
        SELECT id, name, register_no, department, section
        FROM students
        ORDER BY register_no
    """).fetchall()
    conn.close()
    return render_template("students.html", students=rows)


@app.route("/add_student", methods=["POST"])
def add_student():
    if not session.get("admin"):
        return redirect(url_for("home"))

    name = request.form["name"].strip()
    register_no = request.form["register_no"].strip()
    department = request.form["department"]
    section = request.form["section"]

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO students(name, register_no, department, section, password)
            VALUES (?, ?, ?, ?, ?)
        """, (name, register_no, department, section, register_no))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return "Register number already exists."
    conn.close()
    return redirect(url_for("students"))


@app.route("/delete_student/<int:student_id>", methods=["POST"])
def delete_student(student_id):
    if not session.get("admin"):
        return redirect(url_for("home"))

    conn = get_db()
    conn.execute("DELETE FROM attendance WHERE student_id=?", (student_id,))
    conn.execute("DELETE FROM students WHERE id=?", (student_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("students"))


# ---------------- ATTENDANCE ----------------

@app.route("/attendance")
def attendance():
    if not session.get("admin"):
        return redirect(url_for("home"))

    department = request.args.get("department")
    section = request.args.get("section")
    date = request.args.get("date")
    students = []

    if department and section and date:
        conn = get_db()
        students = conn.execute("""
            SELECT s.id, s.register_no, s.name, s.department, s.section,
                   a.status
            FROM students s
            LEFT JOIN attendance a
              ON s.id=a.student_id AND a.attendance_date=?
            WHERE s.department=? AND s.section=?
            ORDER BY s.register_no
        """, (date, department, section)).fetchall()
        conn.close()

    return render_template(
        "attendance.html",
        students=students,
        department=department,
        section=section,
        date=date
    )


@app.route("/attendance_entry")
def attendance_entry():
    # Editable attendance page used for marking and editing attendance.
    if not session.get("admin"):
        return redirect(url_for("home"))

    date = (request.args.get("date") or "").strip()[:10]
    department = request.args.get("department")
    section = request.args.get("section")

    students = []
    if date and department and section:
        conn = get_db()
        students = conn.execute("""
            SELECT s.id, s.name, s.register_no, s.department, s.section, a.status
            FROM students s
            LEFT JOIN attendance a
              ON s.id = a.student_id AND a.attendance_date = ?
            WHERE s.department=? AND s.section=?
            ORDER BY s.register_no
        """, (date, department, section)).fetchall()
        conn.close()

    return render_template(
        "attendance_entry.html",
        students=students,
        date=date,
        department=department,
        section=section
    )


@app.route("/mark_attendance", methods=["POST"])
def mark_attendance():
    if not session.get("admin"):
        return redirect(url_for("home"))

    attendance_date = request.form.get("attendance_date", "").strip()[:10]
    department = request.form.get("department")
    section = request.form.get("section")

    if len(attendance_date) != 10 or not department or not section:
        return "Invalid attendance details."

    conn = get_db()
    cur = conn.cursor()

    student_rows = cur.execute("""
        SELECT id FROM students
        WHERE department=? AND section=?
        ORDER BY register_no
    """, (department, section)).fetchall()

    for row in student_rows:
        student_id = row["id"]
        status = request.form.get(f"status_{student_id}", "Absent")
        if status not in ("Present", "Absent"):
            status = "Absent"

        # One student + one date = one attendance record.
        cur.execute("""
            INSERT INTO attendance(student_id, attendance_date, status)
            VALUES (?, ?, ?)
            ON CONFLICT(student_id, attendance_date)
            DO UPDATE SET status=excluded.status
        """, (student_id, attendance_date, status))

    conn.commit()
    conn.close()

    return redirect(url_for(
        "attendance",
        date=attendance_date,
        department=department,
        section=section,
        saved=1
    ))


@app.route("/view_attendance")
def view_attendance():
    if not session.get("admin"):
        return redirect(url_for("home"))

    conn = get_db()
    rows = conn.execute("""
        SELECT s.name, s.register_no, s.department, s.section,
               a.attendance_date, a.status
        FROM attendance a
        JOIN students s ON a.student_id=s.id
        ORDER BY a.attendance_date DESC, s.register_no
    """).fetchall()
    conn.close()
    return render_template("view_attendance.html", attendance=rows)


# ---------------- REPORT ----------------

@app.route("/report")
def report():
    if not session.get("admin"):
        return redirect(url_for("home"))

    department = request.args.get("department")
    section = request.args.get("section")
    reports = []

    conn = get_db()
    if department and section:
        reports = conn.execute("""
            SELECT s.name, s.register_no,
                   COALESCE(SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END),0) AS present,
                   COUNT(a.id) AS total
            FROM students s
            LEFT JOIN attendance a ON s.id=a.student_id
            WHERE s.department=? AND s.section=?
            GROUP BY s.id
            ORDER BY s.register_no
        """, (department, section)).fetchall()
    conn.close()

    report_rows = []
    for row in reports:
        total = row["total"]
        present = row["present"]
        percentage = round((present / total) * 100, 2) if total else 0
        report_rows.append({
            "name": row["name"],
            "register_no": row["register_no"],
            "present": present,
            "total": total,
            "percentage": percentage
        })

    return render_template(
        "report.html",
        reports=report_rows,
        department=department,
        section=section
    )


setup_database()

if __name__ == "__main__":
    app.run(debug=True)

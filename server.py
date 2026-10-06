#!/usr/bin/env python3
"""Sistema de turnos multi-negocio (MVP). Solo librería estándar de Python + SQLite.
Cada tabla lleva business_id => listo para multi-tenant."""
import os, json, sqlite3, csv, io, hmac, re, datetime as dt
from collections import Counter
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = dt.timezone(dt.timedelta(hours=-3))  # Argentina
LONG = 240  # servicios >= 4h pueden cruzar el corte de mediodía
ACTIVE = ("reservado", "confirmado", "en_espera", "atendido")
STATES = ACTIVE + ("cancelado", "no_asistio")
PAYS = ("efectivo", "transferencia", "debito", "credito", "mercadopago", "otro")
ACT_SQL = "('reservado','confirmado','en_espera','atendido')"

SCHEMA = """
create table if not exists businesses(id integer primary key,slug text unique,name text,phone text,address text,
  slot_min integer default 30,reminders_on integer default 0,inactive_days integer default 60);
create table if not exists staff(id integer primary key,business_id int,name text,does_female int default 0,active int default 1);
create table if not exists services(id integer primary key,business_id int,name text,price int,duration_min int,category text default 'M',active int default 1);
create table if not exists hours(id integer primary key,business_id int,weekday int,start_min int,end_min int);
create table if not exists clients(id integer primary key,business_id int,phone text,name text,created text,unique(business_id,phone));
create table if not exists appointments(id integer primary key,business_id int,staff_id int,client_id int,service_id int,
  date text,start_min int,end_min int,status text default 'reservado',price int,pay_method text,walkin int default 0,created text);
create unique index if not exists ux_slot on appointments(business_id,staff_id,date,start_min)
  where status in ('reservado','confirmado','en_espera','atendido');
create table if not exists blocks(id integer primary key,business_id int,staff_id int,date text,start_min int,end_min int,reason text);
create table if not exists expenses(id integer primary key,business_id int,date text,kind text,category text,description text,amount int);
create table if not exists equipment(id integer primary key,business_id int,name text,cost int,buy_date text,est_cuts int);
create table if not exists reminders(id integer primary key,business_id int,appointment_id int,kind text,send_at text,status text default 'pendiente');
"""


class Err(Exception):
    def __init__(s, code, msg):
        s.code, s.msg = code, msg


def db():
    c = sqlite3.connect(os.environ.get("DB_PATH", os.path.join(HERE, "data.db")), isolation_level=None, timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    if not c.execute("select 1 from businesses").fetchone():
        c.execute("insert into businesses(slug,name) values('sinpelosenlalengua','SinPelosenlaLengua')")
        for wd in (1, 2, 3):
            c.executemany("insert into hours(business_id,weekday,start_min,end_min) values(1,?,?,?)", [(wd, 510, 780), (wd, 1020, 1290)])
        for wd in (4, 5):
            c.executemany("insert into hours(business_id,weekday,start_min,end_min) values(1,?,?,?)", [(wd, 510, 780), (wd, 990, 1320)])
        c.executemany("insert into staff(business_id,name,does_female) values(1,?,?)", [("Mameli", 1), ("Demonti", 0), ("Quilino", 0)])
        c.execute("insert into services(business_id,name,price,duration_min,category) values(1,'Corte',15000,30,'M')")
        # Precio 0: el dueño debe cargarlo en Configuración (no inventamos un precio)
        c.execute("insert into services(business_id,name,price,duration_min,category) values(1,'Trabajo femenino',0,300,'F')")
    return c


def hm(m): return f"{m // 60:02d}:{m % 60:02d}"
def pm(s): h, m = str(s).split(":"); return int(h) * 60 + int(m)
def rows(cur): return [dict(r) for r in cur]


def norm_phone(p):
    d = re.sub(r"\D", "", str(p or ""))
    if d.startswith("00"): d = d[2:]
    if d.startswith("0"): d = d[1:]
    if len(d) == 10: d = "549" + d
    elif d.startswith("54") and len(d) == 12: d = "549" + d[2:]
    if not 10 <= len(d) <= 15: raise Err(400, "Teléfono inválido")
    return d


def busy(c, bid, staff, date):
    a = [tuple(x) for x in c.execute(f"select start_min,end_min from appointments where business_id=? and staff_id=? and date=? and status in {ACT_SQL}", (bid, staff, date))]
    a += [tuple(x) for x in c.execute("select start_min,end_min from blocks where business_id=? and (staff_id=? or staff_id is null) and date=?", (bid, staff, date))]
    return a


def slots(c, b, date, staff, dur):
    d = dt.date.fromisoformat(date)
    now = dt.datetime.now(TZ)
    if d < now.date(): return []
    rng = [(r[0], r[1]) for r in c.execute("select start_min,end_min from hours where business_id=? and weekday=? order by start_min", (b["id"], d.weekday()))]
    if not rng: return []
    used, last, out = busy(c, b["id"], staff, date), rng[-1][1], []
    for a, z in rng:
        for s in range(a, z, b["slot_min"]):
            e = s + dur
            if e > z and not (dur >= LONG and e <= last): continue
            if d == now.date() and s <= now.hour * 60 + now.minute: continue
            if any(s < y and e > x for x, y in used): continue
            out.append(hm(s))
    return out


def create_appt(c, b, staff_id, service_id, date, start, name, phone, public=True, price=None, pay=None, status="reservado", walkin=0):
    name = " ".join(str(name).split())
    if not 2 <= len(name) <= 80: raise Err(400, "Nombre inválido")
    phone = norm_phone(phone)
    d = dt.date.fromisoformat(date); s = pm(start)
    c.execute("BEGIN IMMEDIATE")  # serializa escrituras: evita doble reserva
    try:
        st = c.execute("select * from staff where id=? and business_id=? and active=1", (staff_id, b["id"])).fetchone()
        sv = c.execute("select * from services where id=? and business_id=? and active=1", (service_id, b["id"])).fetchone()
        if not st or not sv: raise Err(400, "Peluquero o servicio inválido")
        if sv["category"] == "F" and not st["does_female"]: raise Err(400, "Ese peluquero no realiza ese servicio")
        e = s + sv["duration_min"]
        if public:
            if d > dt.datetime.now(TZ).date() + dt.timedelta(days=90): raise Err(400, "Fecha demasiado lejana")
            if start not in slots(c, b, date, staff_id, sv["duration_min"]): raise Err(409, "Ese horario ya no está disponible")
        elif any(s < y and e > x for x, y in busy(c, b["id"], staff_id, date)):
            raise Err(409, "Ese horario se superpone con otro turno o bloqueo")
        cl = c.execute("select id from clients where business_id=? and phone=?", (b["id"], phone)).fetchone()
        if cl:
            c.execute("update clients set name=? where id=?", (name, cl["id"])); cid = cl["id"]  # nombre más reciente
        else:
            cid = c.execute("insert into clients(business_id,phone,name,created) values(?,?,?,?)", (b["id"], phone, name, dt.datetime.now(TZ).isoformat())).lastrowid
        if status not in STATES: raise Err(400, "Estado inválido")
        if pay and pay not in PAYS: raise Err(400, "Forma de pago inválida")
        pr = int(sv["price"] if price in (None, "") else price)
        if pr < 0: raise Err(400, "Precio inválido")
        try:
            aid = c.execute("insert into appointments(business_id,staff_id,client_id,service_id,date,start_min,end_min,status,price,pay_method,walkin,created) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (b["id"], staff_id, cid, service_id, date, s, e, status, pr, pay, walkin, dt.datetime.now(TZ).isoformat())).lastrowid
        except sqlite3.IntegrityError:
            raise Err(409, "Ese horario ya no está disponible")
        if not walkin:
            t0 = dt.datetime.combine(d, dt.time(s // 60, s % 60))
            for k, h in (("24h", 24), ("4h", 4), ("1h", 1)):
                c.execute("insert into reminders(business_id,appointment_id,kind,send_at) values(?,?,?,?)", (b["id"], aid, k, (t0 - dt.timedelta(hours=h)).isoformat()))
        c.execute("COMMIT")
        return {"ok": True, "id": aid, "peluquero": st["name"], "fecha": date, "hora": start, "servicio": sv["name"], "cliente": name}
    except BaseException:
        if c.in_transaction: c.execute("ROLLBACK")
        raise


# ---------- público ----------
def r_config(c, b, q, body):
    return {"negocio": {k: b[k] for k in ("name", "phone", "address", "slot_min")},
            "staff": rows(c.execute("select id,name,does_female from staff where business_id=? and active=1", (b["id"],))),
            "services": rows(c.execute("select id,name,price,duration_min,category from services where business_id=? and active=1", (b["id"],)))}


def r_avail(c, b, q, body):
    sv = c.execute("select duration_min from services where id=? and business_id=? and active=1", (q.get("service_id"), b["id"])).fetchone()
    if not sv: raise Err(400, "Servicio inválido")
    return {"slots": slots(c, b, q["date"], int(q["staff_id"]), sv["duration_min"])}


def r_book(c, b, q, x):
    return create_appt(c, b, int(x["staff_id"]), int(x["service_id"]), x["date"], x["start"], f'{x.get("name", "")} {x.get("surname", "")}', x.get("phone"))


# ---------- admin ----------
def r_agenda(c, b, q, body):
    sql = """select a.id,a.date,a.start_min,a.end_min,a.status,a.price,a.pay_method,a.walkin,s.id staff_id,s.name staff,cl.name client,cl.phone,sv.name service,sv.category
      from appointments a join staff s on s.id=a.staff_id join clients cl on cl.id=a.client_id join services sv on sv.id=a.service_id
      where a.business_id=? and a.date between ? and ? order by a.date,a.start_min"""
    out = rows(c.execute(sql, (b["id"], q.get("from", "2000-01-01"), q.get("to", "2100-01-01"))))
    for a in out: a["start"], a["end"] = hm(a["start_min"]), hm(a["end_min"])
    return out


def r_status(c, b, q, x):
    if x["status"] not in STATES: raise Err(400, "Estado inválido")
    if x.get("pay_method") and x["pay_method"] not in PAYS: raise Err(400, "Forma de pago inválida")
    c.execute("update appointments set status=?,pay_method=coalesce(?,pay_method) where id=? and business_id=?", (x["status"], x.get("pay_method"), x["id"], b["id"]))
    return {"ok": True}


def r_walkin(c, b, q, x):
    now = dt.datetime.now(TZ)
    return create_appt(c, b, int(x["staff_id"]), int(x["service_id"]), x.get("date") or now.date().isoformat(), x.get("start") or now.strftime("%H:%M"),
                       x["name"], x["phone"], public=False, price=x.get("price"), pay=x.get("pay_method") or "efectivo", status="atendido", walkin=1)


def r_blocks(c, b, q, body):
    return rows(c.execute("select k.*,s.name staff from blocks k left join staff s on s.id=k.staff_id where k.business_id=? order by date desc,start_min", (b["id"],)))


def r_block_add(c, b, q, x):
    s, e = pm(x["start"]), pm(x["end"])
    if e <= s: raise Err(400, "La hora final debe ser posterior")
    dt.date.fromisoformat(x["date"])
    c.execute("insert into blocks(business_id,staff_id,date,start_min,end_min,reason) values(?,?,?,?,?,?)", (b["id"], x.get("staff_id") or None, x["date"], s, e, str(x.get("reason", ""))[:100]))
    return {"ok": True}


def r_block_del(c, b, q, x):
    c.execute("delete from blocks where id=? and business_id=?", (x["id"], b["id"])); return {"ok": True}


def r_expenses(c, b, q, body):
    return rows(c.execute("select * from expenses where business_id=? and date between ? and ? order by date desc", (b["id"], q.get("from", "2000-01-01"), q.get("to", "2100-01-01"))))


def r_expense_add(c, b, q, x):
    if x["kind"] not in ("fijo", "variable", "otro"): raise Err(400, "Tipo inválido")
    if int(x["amount"]) <= 0: raise Err(400, "Importe inválido")
    dt.date.fromisoformat(x["date"])
    c.execute("insert into expenses(business_id,date,kind,category,description,amount) values(?,?,?,?,?,?)", (b["id"], x["date"], x["kind"], str(x.get("category", ""))[:40], str(x.get("description", ""))[:200], int(x["amount"])))
    return {"ok": True}


def r_equipment_add(c, b, q, x):
    if int(x["cost"]) <= 0 or int(x["est_cuts"]) <= 0: raise Err(400, "Costo y cortes estimados deben ser > 0")
    c.execute("insert into equipment(business_id,name,cost,buy_date,est_cuts) values(?,?,?,?,?)", (b["id"], x["name"], int(x["cost"]), x.get("buy_date"), int(x["est_cuts"])))
    return {"ok": True}


def r_business(c, b, q, x):
    if x.get("get"): return dict(b) | {"staff": rows(c.execute("select * from staff where business_id=?", (b["id"],))), "services": rows(c.execute("select * from services where business_id=?", (b["id"],))), "equipment": rows(c.execute("select * from equipment where business_id=?", (b["id"],)))}
    if int(x["slot_min"]) < 5: raise Err(400, "Duración inválida")
    c.execute("update businesses set name=?,phone=?,address=?,slot_min=?,reminders_on=?,inactive_days=? where id=?", (x["name"], x.get("phone"), x.get("address"), int(x["slot_min"]), int(bool(x.get("reminders_on"))), int(x["inactive_days"]), b["id"]))
    return {"ok": True}


def r_staff_save(c, b, q, x):  # nunca se borra: se desactiva
    if x.get("id"): c.execute("update staff set name=?,does_female=?,active=? where id=? and business_id=?", (x["name"], int(bool(x["does_female"])), int(bool(x["active"])), x["id"], b["id"]))
    else: c.execute("insert into staff(business_id,name,does_female) values(?,?,?)", (b["id"], x["name"], int(bool(x.get("does_female")))))
    return {"ok": True}


def r_service_save(c, b, q, x):
    if x["category"] not in ("M", "F", "any") or int(x["price"]) < 0 or int(x["duration_min"]) < 5: raise Err(400, "Datos inválidos")
    v = (x["name"], int(x["price"]), int(x["duration_min"]), x["category"])
    if x.get("id"): c.execute("update services set name=?,price=?,duration_min=?,category=?,active=? where id=? and business_id=?", v + (int(bool(x.get("active", 1))), x["id"], b["id"]))
    else: c.execute("insert into services(name,price,duration_min,category,business_id) values(?,?,?,?,?)", v + (b["id"],))
    return {"ok": True}


def r_clients(c, b, q, body):
    out = {}
    for a in c.execute("select a.*,cl.name cname,cl.phone,s.name staff,sv.name service from appointments a join clients cl on cl.id=a.client_id join staff s on s.id=a.staff_id join services sv on sv.id=a.service_id where a.business_id=?", (b["id"],)):
        o = out.setdefault(a["client_id"], {"nombre": a["cname"], "telefono": a["phone"], "visitas": 0, "gastado": 0, "primera": None, "ultima": None, "cancelaciones": 0, "ausencias": 0, "_s": Counter(), "_v": Counter(), "_d": Counter()})
        if a["status"] == "cancelado": o["cancelaciones"] += 1
        elif a["status"] == "no_asistio": o["ausencias"] += 1
        elif a["status"] == "atendido":
            o["visitas"] += 1; o["gastado"] += a["price"]; o["_s"][a["staff"]] += 1; o["_v"][a["service"]] += 1; o["_d"][dt.date.fromisoformat(a["date"]).strftime("%A")] += 1
            o["primera"] = min(o["primera"] or a["date"], a["date"]); o["ultima"] = max(o["ultima"] or a["date"], a["date"])
    res, qq = [], (q.get("q") or "").lower()
    for o in out.values():
        o["peluquero_habitual"] = (o.pop("_s").most_common(1) or [[None]])[0][0]; o["servicio_habitual"] = (o.pop("_v").most_common(1) or [[None]])[0][0]; o["dia_habitual"] = (o.pop("_d").most_common(1) or [[None]])[0][0]
        o["frecuencia_dias"] = round((dt.date.fromisoformat(o["ultima"]) - dt.date.fromisoformat(o["primera"])).days / (o["visitas"] - 1), 1) if o["visitas"] > 1 else None
        if not qq or qq in o["nombre"].lower() or qq in o["telefono"]: res.append(o)
    return sorted(res, key=lambda o: -o["visitas"])


def r_stats(c, b, q, body):
    bid, f, t = b["id"], q["from"], q["to"]
    today = dt.datetime.now(TZ).date()
    ap = rows(c.execute("select a.*,sv.category cat from appointments a join services sv on sv.id=a.service_id where a.business_id=? and a.date between ? and ?", (bid, f, t)))
    done = [a for a in ap if a["status"] == "atendido"]
    booked = [a for a in ap if a["status"] in ACTIVE]
    wh = {r[0]: r[1] for r in c.execute("select weekday,sum(end_min-start_min) from hours where business_id=? group by weekday", (bid,))}
    days, d = [], dt.date.fromisoformat(f)
    while d <= dt.date.fromisoformat(t): days.append(d); d += dt.timedelta(1)
    base = sum(wh.get(x.weekday(), 0) for x in days)
    first = {r[0]: r[1] for r in c.execute("select client_id,min(date) from appointments where business_id=? and status='atendido' group by client_id", (bid,))}
    exp = rows(c.execute("select * from expenses where business_id=? and date between ? and ?", (bid, f, t)))
    egresos = sum(e["amount"] for e in exp)
    ing = sum(a["price"] for a in done)
    byk = lambda k: sum(e["amount"] for e in exp if e["kind"] == k)
    n = len(done)
    dep = sum(e["cost"] / e["est_cuts"] for e in c.execute("select cost,est_cuts from equipment where business_id=?", (bid,)))
    ticket = ing / n if n else 0
    per_staff = []
    for s in c.execute("select * from staff where business_id=?", (bid,)):
        mine = [a for a in booked if a["staff_id"] == s["id"]]
        dn = [a for a in done if a["staff_id"] == s["id"]]
        blk = sum(r[0] for r in c.execute("select sum(end_min-start_min) from blocks where business_id=? and (staff_id=? or staff_id is null) and date between ? and ?", (bid, s["id"], f, t)) if r[0])
        av = max(base - blk, 0); used = sum(a["end_min"] - a["start_min"] for a in mine)
        if not s["active"] and not mine: continue
        cl = {a["client_id"] for a in dn}
        newc = {x for x in cl if f <= first[x] <= t}
        per_staff.append({"peluquero": s["name"], "cortes": len(dn), "facturacion": sum(a["price"] for a in dn), "ticket": round(sum(a["price"] for a in dn) / len(dn)) if dn else 0,
                          "horas_trabajadas": round(used / 60, 1), "horas_disponibles": round(av / 60, 1), "horas_ociosas": round(max(av - used, 0) / 60, 1),
                          "ocupacion_pct": round(100 * used / av, 1) if av else 0, "hombres": sum(a["cat"] != "F" for a in dn), "mujeres": sum(a["cat"] == "F" for a in dn),
                          "clientes_nuevos": len(newc), "clientes_recurrentes": len(cl - newc)})
    cl_all = {a["client_id"] for a in done}
    newc = {x for x in cl_all if f <= first[x] <= t}
    av_tot = sum(p["horas_disponibles"] for p in per_staff); used_tot = sum(p["horas_trabajadas"] for p in per_staff)
    cap = int(av_tot * 60 / b["slot_min"])
    inact = rows(c.execute("select cl.name,cl.phone,max(a.date) ultima,count(*) visitas from clients cl join appointments a on a.client_id=cl.id and a.status='atendido' where cl.business_id=? group by cl.id having max(a.date)<? order by ultima", (bid, (today - dt.timedelta(days=int(q.get("inactive_days") or b["inactive_days"]))).isoformat())))
    adv = [(dt.date.fromisoformat(a["date"]) - dt.date.fromisoformat(a["created"][:10])).days for a in booked if not a["walkin"]]
    canc = [a for a in ap if a["status"] == "cancelado"]; nos = [a for a in ap if a["status"] == "no_asistio"]
    return {"desde": f, "hasta": t,
            "caja": {"ingresos": ing, "ingresos_con_turno": sum(a["price"] for a in done if not a["walkin"]), "ingresos_sin_turno": sum(a["price"] for a in done if a["walkin"]),
                     "por_medio": dict(Counter({m: sum(a["price"] for a in done if (a["pay_method"] or "sin_dato") == m) for m in {a["pay_method"] or "sin_dato" for a in done}})),
                     "egresos": egresos, "neto": ing - egresos, "gastos": sorted(exp, key=lambda e: e["date"])},
            "rentabilidad": {"fijos": byk("fijo"), "variables": byk("variable"), "otros": byk("otro"), "depreciacion_por_corte": round(dep, 2), "ticket": round(ticket),
                             "ganancia_por_corte": round(ticket - (byk("variable") + byk("fijo")) / n - dep) if n else None},
            "kpi": {"cortes": n, "facturacion": ing, "ticket_promedio": round(ticket), "clientes_nuevos": len(newc), "clientes_recurrentes": len(cl_all - newc),
                    "ocupacion_pct": round(100 * used_tot / av_tot, 1) if av_tot else 0, "horas_trabajadas": used_tot, "horas_disponibles": av_tot, "horas_ociosas": round(max(av_tot - used_tot, 0), 1),
                    "capacidad_max_cortes": cap, "capacidad_usada_pct": round(100 * len(booked) / cap, 1) if cap else 0, "potencial_facturacion_extra": round(max(cap - len(booked), 0) * ticket),
                    "cancelaciones": len(canc), "cancelaciones_pct": round(100 * len(canc) / len(ap), 1) if ap else 0, "no_asistio": len(nos),
                    "anticipacion_promedio_dias": round(sum(adv) / len(adv), 1) if adv else None},
            "demanda_por_hora": dict(sorted(Counter(hm(a["start_min"] // 60 * 60) for a in booked).items())),
            "demanda_por_dia": dict(Counter(dt.date.fromisoformat(a["date"]).strftime("%A") for a in booked)),
            "por_peluquero": per_staff, "clientes_inactivos": inact}


def r_csv(c, b, q, body):
    qs = {"turnos": "select a.date,a.start_min,s.name peluquero,cl.name cliente,cl.phone telefono,sv.name servicio,a.price precio,a.pay_method pago,a.status estado from appointments a join staff s on s.id=a.staff_id join clients cl on cl.id=a.client_id join services sv on sv.id=a.service_id where a.business_id=? order by a.date",
          "clientes": "select name,phone,created from clients where business_id=?", "gastos": "select date,kind,category,description,amount from expenses where business_id=?"}
    cur = c.execute(qs[q["name"]], (b["id"],)); out = io.StringIO(); w = csv.writer(out)
    w.writerow([x[0] for x in cur.description]); w.writerows(cur.fetchall())
    return out.getvalue()


ROUTES = {("GET", "/api/public/config"): (r_config, 0), ("GET", "/api/public/availability"): (r_avail, 0), ("POST", "/api/public/book"): (r_book, 0),
          ("GET", "/api/admin/agenda"): (r_agenda, 1), ("POST", "/api/admin/status"): (r_status, 1), ("POST", "/api/admin/walkin"): (r_walkin, 1),
          ("GET", "/api/admin/blocks"): (r_blocks, 1), ("POST", "/api/admin/block"): (r_block_add, 1), ("POST", "/api/admin/block_delete"): (r_block_del, 1),
          ("GET", "/api/admin/expenses"): (r_expenses, 1), ("POST", "/api/admin/expense"): (r_expense_add, 1), ("POST", "/api/admin/equipment"): (r_equipment_add, 1),
          ("POST", "/api/admin/business"): (r_business, 1), ("POST", "/api/admin/staff"): (r_staff_save, 1), ("POST", "/api/admin/service"): (r_service_save, 1),
          ("GET", "/api/admin/clients"): (r_clients, 1), ("GET", "/api/admin/stats"): (r_stats, 1), ("GET", "/api/admin/csv"): (r_csv, 1)}
WRITES = {r_book, r_status, r_walkin, r_block_add, r_block_del, r_expense_add, r_equipment_add, r_business, r_staff_save, r_service_save}


class H(BaseHTTPRequestHandler):
    def log_message(s, *a): pass

    def out(s, code, body, ct="application/json"):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode()
        s.send_response(code); s.send_header("Content-Type", ct + "; charset=utf-8"); s.send_header("Content-Length", str(len(data)))
        s.send_header("X-Content-Type-Options", "nosniff"); s.send_header("Cache-Control", "no-store"); s.end_headers(); s.wfile.write(data)

    def go(s, method):
        u = urlparse(s.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if method == "GET" and not u.path.startswith("/api/"):
            f = {"/": "index.html", "/admin": "admin.html"}.get(u.path)
            if not f: return s.out(404, {"error": "No encontrado"})
            return s.out(200, open(os.path.join(HERE, "static", f), "rb").read(), "text/html")
        route = ROUTES.get((method, u.path))
        if not route: return s.out(404, {"error": "No encontrado"})
        fn, adm = route
        if adm and not hmac.compare_digest(s.headers.get("X-Admin-Token", "").encode(), os.environ.get("ADMIN_PASSWORD", "admin123").encode()):
            return s.out(401, {"error": "No autorizado"})
        body = {}
        if method == "POST":
            n = int(s.headers.get("Content-Length") or 0)
            if n > 100000: return s.out(413, {"error": "Demasiado grande"})
            try: body = json.loads(s.rfile.read(n) or b"{}")
            except ValueError: return s.out(400, {"error": "JSON inválido"})
        c = db()
        try:
            slug = q.get("b"); b = c.execute("select * from businesses where slug=?", (slug,)).fetchone() if slug else c.execute("select * from businesses order by id").fetchone()
            if not b: raise Err(404, "Negocio inexistente")
            res = fn(c, b, q, body)
            s.out(200, res, "text/csv" if isinstance(res, str) else "application/json")
        except Err as e: s.out(e.code, {"error": e.msg})
        except (ValueError, KeyError, TypeError): s.out(400, {"error": "Datos inválidos"})
        finally: c.close()

    def do_GET(s): s.go("GET")
    def do_POST(s): s.go("POST")


if __name__ == "__main__":
    db().close()
    if os.environ.get("ADMIN_PASSWORD", "admin123") == "admin123": print("⚠ Usando clave de admin por defecto. Definí ADMIN_PASSWORD.")
    port = int(os.environ.get("PORT", 8000)); print(f"http://localhost:{port}  (admin: /admin)")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()

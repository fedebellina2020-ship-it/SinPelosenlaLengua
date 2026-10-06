import os, tempfile, threading, json, datetime as dt, urllib.request, urllib.error
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db"); os.environ["ADMIN_PASSWORD"] = "clave"
import server
from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(("127.0.0.1", 8099), server.H); threading.Thread(target=srv.serve_forever, daemon=True).start()
U = "http://127.0.0.1:8099"


def call(p, body=None, adm=False):
    r = urllib.request.Request(U + p, data=json.dumps(body).encode() if body is not None else None, headers={"X-Admin-Token": "clave" if adm else "", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as f: raw = f.read().decode(); return f.status, (json.loads(raw) if "json" in f.headers["Content-Type"] else raw)
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())


def nxt(wd):  # próxima fecha con ese weekday (0=lunes), al menos 7 días adelante
    d = dt.date.today() + dt.timedelta(days=7)
    while d.weekday() != wd: d += dt.timedelta(1)
    return d.isoformat()


ok = lambda n, c: print(("OK  " if c else "FALLA"), n) or (c or exit(1))
cfg = call("/api/public/config")[1]
S = {s["name"]: s["id"] for s in cfg["staff"]}; V = {s["name"]: s["id"] for s in cfg["services"]}
mie, lun, vie = nxt(2), nxt(0), nxt(4)
sl = lambda d, st, sv: call(f"/api/public/availability?date={d}&staff_id={S[st]}&service_id={V[sv]}")[1]["slots"]

ok("lunes cerrado", sl(lun, "Mameli", "Corte") == [])
m = sl(mie, "Demonti", "Corte")
ok("miércoles 08:30-12:30 y 17:00-21:00, turnos de 30min", m[0] == "08:30" and m[7] == "12:00" and "13:00" not in m and "17:00" in m and m[-1] == "21:00")
ok("viernes tarde abre 16:30 y cierra 21:30", "16:30" in sl(vie, "Demonti", "Corte") and sl(vie, "Demonti", "Corte")[-1] == "21:30")
ok("mujer sólo con Mameli", call("/api/public/book", {"staff_id": S["Demonti"], "service_id": V["Trabajo femenino"], "date": mie, "start": "08:30", "name": "Ana", "surname": "Gómez", "phone": "351 600 0000"})[0] == 400)
st, r = call("/api/public/book", {"staff_id": S["Mameli"], "service_id": V["Trabajo femenino"], "date": mie, "start": "08:30", "name": "Ana", "surname": "Gómez", "phone": "351 600 0000"})
ok("reserva femenina 5h de Mameli", st == 200)
mm = sl(mie, "Mameli", "Corte")
ok("Mameli bloqueado 08:30-13:30 (tarde libre), otros peluqueros libres", mm[0] == "17:00" and sl(mie, "Demonti", "Corte")[0] == "08:30")
ok("no se puede reservar el trabajo largo encima", call("/api/public/book", {"staff_id": S["Mameli"], "service_id": V["Corte"], "date": mie, "start": "12:00", "name": "Pedro", "surname": "Lopez", "phone": "3516111111"})[0] == 409)
b = {"staff_id": S["Demonti"], "service_id": V["Corte"], "date": mie, "start": "18:30", "name": "Juan", "surname": "Pérez", "phone": "+54 9 351 123-4567"}
ok("reserva simple", call("/api/public/book", b)[0] == 200)
ok("doble reserva rechazada (409)", call("/api/public/book", dict(b, name="Otro", phone="3517000000"))[0] == 409)
# concurrencia
res = []; ts = [threading.Thread(target=lambda i=i: res.append(call("/api/public/book", dict(b, start="19:00", name=f"C{i}", surname="X", phone=f"351800000{i}"))[0])) for i in range(10)]
[t.start() for t in ts]; [t.join() for t in ts]
ok(f"10 reservas simultáneas del mismo horario -> exactamente 1 gana {sorted(res)}", res.count(200) == 1 and res.count(409) == 9)
call("/api/public/book", dict(b, start="19:30", name="Juan", surname="Perez", phone="0351 1234567"))
st, cl = call("/api/admin/clients", adm=True)
ok("mismo teléfono con distintos nombres/formatos = un solo cliente", sum(1 for x in cl if x["telefono"] == "5493511234567") == 1)
ok("admin sin clave -> 401", call("/api/admin/agenda")[0] == 401)
call("/api/admin/block", {"staff_id": S["Quilino"], "date": mie, "start": "17:00", "end": "19:00", "reason": "Trámite"}, True)
q = sl(mie, "Quilino", "Corte")
ok("bloqueo oculta 17:00-19:00", "17:00" not in q and "18:30" not in q and "19:00" in q)
call("/api/admin/service", {"id": V["Trabajo femenino"], "name": "Trabajo femenino", "price": 60000, "duration_min": 300, "category": "F", "active": 1}, True)
# caja / rentabilidad / stats
w = call("/api/admin/walkin", {"staff_id": S["Quilino"], "service_id": V["Corte"], "name": "Luis Ruiz", "phone": "3519990000", "price": 14000, "pay_method": "transferencia", "date": mie, "start": "09:00"}, True)
ok("corte sin turno con precio $14.000", w[0] == 200)
ag = call(f"/api/admin/agenda?from={mie}&to={mie}", adm=True)[1]
jid = [a for a in ag if a["client"].startswith("Juan")][0]["id"]
call("/api/admin/status", {"id": jid, "status": "atendido", "pay_method": "efectivo"}, True)
call("/api/admin/expense", {"date": mie, "kind": "fijo", "category": "alquiler", "description": "", "amount": 10000}, True)
call("/api/admin/expense", {"date": mie, "kind": "variable", "category": "insumos", "description": "", "amount": 2000}, True)
call("/api/admin/equipment", {"name": "Máquina", "cost": 300000, "est_cuts": 5000}, True)
s = call(f"/api/admin/stats?from={mie}&to={mie}", adm=True)[1]
ok("ingresos = 14.000 + 15.000", s["caja"]["ingresos"] == 29000 and s["caja"]["por_medio"]["transferencia"] == 14000)
ok("neto = ingresos - egresos (29000-12000)", s["caja"]["neto"] == 17000)
ok("depreciación $60/corte y ganancia/corte = 14500-6000-60=8440", s["rentabilidad"]["depreciacion_por_corte"] == 60 and s["rentabilidad"]["ganancia_por_corte"] == 8440)
ok("estadísticas: 2 cortes, ticket 14.500, horas ociosas calculadas", s["kpi"]["cortes"] == 2 and s["kpi"]["ticket_promedio"] == 14500 and s["kpi"]["horas_ociosas"] > 0)
ok("exportación CSV", "Juan" in call("/api/admin/csv?name=turnos", adm=True)[1])
print("\nTodas las pruebas pasaron.")

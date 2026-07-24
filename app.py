#!/usr/bin/env python3
"""Linux 企业级 SSD 稳定性与耐久压力测试系统。

默认演示模式绝不写盘。真实裸盘 fio 压测必须同时满足：Linux、root、
ENABLE_DESTRUCTIVE_FIO=1、设备未挂载，以及页面中输入完整的擦除确认短语。
"""
import json
import os
import platform
import random
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT, DATA_FILE = Path(__file__).parent, Path(__file__).parent / "data.json"
LOCK, PROCESSES = threading.Lock(), {}
DEFAULT_PLANS = [
    {"id":"plan-burnin","name":"72 小时耐久老化","duration":72,"block_size":"4K","read_ratio":30,"queue_depth":64,"threshold_temp":70,"description":"随机混合 I/O，验证企业盘长时写入稳定性、温度节流与尾延迟"},
    {"id":"plan-stability","name":"24 小时稳定性验证","duration":24,"block_size":"128K","read_ratio":50,"queue_depth":32,"threshold_temp":65,"description":"平衡读写负载，适用于到货验收、批量抽检"},
    {"id":"plan-spike","name":"突发负载恢复测试","duration":8,"block_size":"4K","read_ratio":20,"queue_depth":128,"threshold_temp":72,"description":"高队列深度脉冲压力，关注延迟尖峰与恢复能力"},
]

def now(): return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
def is_linux(): return platform.system() == "Linux"
def destructive_enabled(): return os.getenv("ENABLE_DESTRUCTIVE_FIO") == "1"
def command(cmd):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=12, check=False)
    except (OSError, subprocess.TimeoutExpired): return None
def flatten(items):
    for item in items:
        yield item
        yield from flatten(item.get("children", []))

def smart(path, transport):
    """读取可选的 NVMe / SMART 遥测；工具缺失或无权限时保留 --。"""
    info={"health":"--","temperature":"--"}
    result = command(["nvme","smart-log",path,"-o","json"]) if transport == "nvme" and shutil.which("nvme") else None
    if result and result.returncode == 0:
        try:
            data=json.loads(result.stdout); info["temperature"]=data.get("temperature", "--")
            info["health"]=data.get("percentage_used", 0); info["health"]=max(0,100-int(info["health"]))
            return info
        except (ValueError, TypeError): pass
    result = command(["smartctl","-A","-j",path]) if shutil.which("smartctl") else None
    if result and result.stdout:
        try:
            data=json.loads(result.stdout); info["temperature"]=data.get("temperature",{}).get("current","--")
            info["health"]=100 if data.get("smart_status",{}).get("passed") else "--"
        except (ValueError, TypeError): pass
    return info

def discover_linux_devices():
    if not is_linux() or not shutil.which("lsblk"): return []
    result=command(["lsblk","--json","--bytes","--output","NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,ROTA,MOUNTPOINTS"])
    if not result or result.returncode: return []
    try: blocks=json.loads(result.stdout).get("blockdevices",[])
    except ValueError: return []
    devices=[]
    for disk in flatten(blocks):
        if disk.get("type") != "disk" or str(disk.get("rota")) not in ("0", "False", "false", "None"): continue
        path=disk.get("path", ""); trans=(disk.get("tran") or "nvme").lower()
        if not path.startswith("/dev/"): continue
        mounts=[m for m in (disk.get("mountpoints") or []) if m]
        health=smart(path, trans)
        devices.append({"id":disk["name"],"path":path,"name":(disk.get("model") or "Enterprise SSD").strip(),"serial":(disk.get("serial") or "未读取").strip(),"interface":"NVMe" if trans=="nvme" else trans.upper(),"capacity":f"{int(disk.get('size',0))/1024**3:.0f} GB","health":health["health"],"temperature":health["temperature"],"mounted":bool(mounts),"mountpoints":mounts,"status":"已挂载（受保护）" if mounts else "可测试"})
    return devices

def demo_devices():
    return [{"id":"demo-nvme0","path":"/dev/nvme0n1","name":"Samsung PM9A3 3.84TB","serial":"DEMO-24001","interface":"NVMe Gen4","capacity":"3.49 TB","health":98,"temperature":38,"mounted":False,"mountpoints":[],"status":"演示设备"}]
def load_state():
    if DATA_FILE.exists():
        try: return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except ValueError: pass
    return {"plans":DEFAULT_PLANS,"tasks":[]}
STATE=load_state(); STATE.setdefault("plans",DEFAULT_PLANS); STATE.setdefault("tasks",[])
def persist(): DATA_FILE.write_text(json.dumps({"plans":STATE["plans"],"tasks":STATE["tasks"]},ensure_ascii=False,indent=2),encoding="utf-8")
def event(task,severity,text): task["events"].append({"time":now(),"severity":severity,"text":text}); task["events"]=task["events"][-50:]
def sample_health(task):
    info=smart(task["path"],task["transport"].lower()); return {"time":now(),"temperature":info["temperature"],"p99":"--","throughput":"--","health":info["health"]}
def finish(task,result):
    task["status"]="已完成"; task["ended_at"]=now(); task["result"]=result; event(task,"信息",f"测试完成，稳定性结论：{result}"); persist()

def demo_runner(task_id):
    random.seed(task_id)
    while True:
        time.sleep(2)
        with LOCK:
            task=next((x for x in STATE["tasks"] if x["id"]==task_id),None)
            if not task or task["status"]!="运行中": return
            task["elapsed"]+=10; task["progress"]=min(100,round(task["elapsed"]/(task["duration"]*60)*100,1)); p=task["progress"]
            s={"time":now(),"temperature":round(38+18*p/100+random.uniform(-2,2),1),"p99":round(3+10*p/100+random.random()*2,2),"throughput":round(2400-420*p/100+random.uniform(-80,80)),"health":round(100-p*.015,2)}; task["samples"].append(s); task["samples"]=task["samples"][-200:]
            if s["temperature"]>=task["threshold_temp"] and not task.get("temp_alerted"): event(task,"严重",f"温度 {s['temperature']}°C 达到阈值 {task['threshold_temp']}°C"); task["temp_alerted"]=True
            if s["p99"]>14 and not task.get("latency_alerted"): event(task,"警告",f"P99 延迟升至 {s['p99']} ms"); task["latency_alerted"]=True
            if p>=100: finish(task,"预警" if task.get("temp_alerted") or task.get("latency_alerted") else "通过"); return
            persist()

def fio_runner(task_id):
    with LOCK:
        task=next(x for x in STATE["tasks"] if x["id"]==task_id); runtime=task["duration"]*3600
        cmd=["fio",f"--name=enterprise-ssd-{task_id}",f"--filename={task['path']}","--direct=1","--ioengine=libaio","--time_based=1",f"--runtime={runtime}",f"--rw=randrw",f"--rwmixread={task['read_ratio']}",f"--bs={task['block_size']}",f"--iodepth={task['queue_depth']}","--numjobs=1","--group_reporting=1","--output-format=json"]
        event(task,"信息",f"已启动 fio 裸盘测试：{task['path']}（该设备上的数据将被覆盖）"); persist()
    try: process=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True); PROCESSES[task_id]=process
    except OSError as exc:
        with LOCK: task["status"]="失败"; task["result"]="执行器错误"; task["ended_at"]=now(); event(task,"严重",str(exc)); persist()
        return
    started=time.time()
    while process.poll() is None:
        time.sleep(15)
        with LOCK:
            if task["status"]!="运行中": process.terminate(); break
            task["elapsed"]=int(time.time()-started); task["progress"]=min(99.9,round(task["elapsed"]/runtime*100,1)); s=sample_health(task); task["samples"].append(s); task["samples"]=task["samples"][-200:]
            try:
                if float(s["temperature"])>=task["threshold_temp"] and not task.get("temp_alerted"): event(task,"严重",f"温度 {s['temperature']}°C 达到阈值"); task["temp_alerted"]=True
            except (ValueError,TypeError): pass
            persist()
    stdout,stderr=process.communicate(); PROCESSES.pop(task_id,None)
    with LOCK:
        if task["status"]=="已停止": return
        if process.returncode:
            task["status"]="失败"; task["result"]="fio 执行失败"; task["ended_at"]=now(); event(task,"严重",(stderr or "fio 返回异常")[-300:]); persist(); return
        try:
            job=json.loads(stdout)["jobs"][0]; p99=max(job["read"].get("clat_ns",{}).get("percentile",{}).get("99.000000",0),job["write"].get("clat_ns",{}).get("percentile",{}).get("99.000000",0))/1e6; bw=(job["read"].get("bw_bytes",0)+job["write"].get("bw_bytes",0))/1e6
            task["samples"].append({"time":now(),"temperature":"--","p99":round(p99,2),"throughput":round(bw,1),"health":"--"})
        except (ValueError,KeyError,IndexError,TypeError): event(task,"警告","fio 已完成，但无法解析部分性能摘要")
        task["progress"]=100; finish(task,"预警" if task.get("temp_alerted") else "通过")

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(ROOT/"static"),**kwargs)
    def log_message(self,*args): pass
    def send_json(self,body,status=200):
        raw=json.dumps(body,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def body(self): return json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))) or b"{}")
    def state(self):
        devices=discover_linux_devices() if is_linux() else demo_devices()
        return {"devices":devices,"plans":STATE["plans"],"tasks":STATE["tasks"],"environment":{"linux":is_linux(),"fio":bool(shutil.which("fio")),"destructive_enabled":destructive_enabled(),"root":getattr(os,"geteuid",lambda:1)()==0}}
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/api/state":
            with LOCK: return self.send_json(self.state())
        if path.startswith("/api/report/"):
            with LOCK:
                task=next((x for x in STATE["tasks"] if x["id"]==path.rsplit("/",1)[-1]),None)
                if not task:return self.send_json({"error":"任务不存在"},404)
                rows="".join(f"<li><b>{e['time']} [{e['severity']}]</b> {e['text']}</li>" for e in task["events"]); last=task["samples"][-1] if task["samples"] else {}
            page=f"<!doctype html><meta charset=utf-8><title>SSD 测试报告</title><style>body{{font:15px Arial;margin:48px;color:#172033}}h1{{color:#1d4ed8}}.c{{border:1px solid #dbe2ef;padding:18px;border-radius:10px;margin:16px 0}}</style><h1>企业级 SSD 稳定性与耐久测试报告</h1><p>生成：{now()} | 模式：{task['mode']}</p><div class=c>设备：{task['device']}（{task['serial']}）<br>路径：{task['path']}<br>策略：{task['plan']} / {task['duration']} 小时<br>结论：{task['status']} / {task['result']}<br>最后遥测：温度 {last.get('temperature','--')}°C，P99 {last.get('p99','--')}ms，吞吐 {last.get('throughput','--')}MB/s</div><div class=c><h2>事件</h2><ul>{rows}</ul></div>".encode()
            self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Disposition",'attachment; filename="enterprise-ssd-report.html"');self.send_header("Content-Length",str(len(page)));self.end_headers();return self.wfile.write(page)
        return super().do_GET()
    def do_POST(self):
        path=urlparse(self.path).path
        try:
            data=self.body()
            with LOCK:
                if path=="/api/tasks":
                    devices=discover_linux_devices() if is_linux() else demo_devices(); device=next((x for x in devices if x["id"]==data.get("device_id")),None); plan=next((x for x in STATE["plans"] if x["id"]==data.get("plan_id")),None); mode=data.get("mode","demo")
                    if not device or not plan:return self.send_json({"error":"请选择有效设备和策略"},400)
                    if mode=="real":
                        phrase=f"ERASE {device['path']}"
                        if not (is_linux() and shutil.which("fio") and destructive_enabled() and getattr(os,"geteuid",lambda:1)()==0): return self.send_json({"error":"真实压测要求 Linux、root、fio 与 ENABLE_DESTRUCTIVE_FIO=1"},403)
                        if device["mounted"]:return self.send_json({"error":f"拒绝：设备已挂载到 {', '.join(device['mountpoints'])}"},403)
                        if data.get("confirmation")!=phrase:return self.send_json({"error":f"确认短语不正确，应为：{phrase}"},403)
                    task={"id":uuid.uuid4().hex[:8],"name":f"{device['name']} · {plan['name']}","device":device["name"],"serial":device["serial"],"path":device["path"],"transport":device["interface"],"plan":plan["name"],"duration":plan["duration"],"block_size":plan["block_size"],"read_ratio":plan["read_ratio"],"queue_depth":plan["queue_depth"],"threshold_temp":plan["threshold_temp"],"mode":"真实 fio 裸盘" if mode=="real" else "安全演示","status":"运行中","result":"--","started_at":now(),"ended_at":None,"elapsed":0,"progress":0,"samples":[],"events":[]}
                    event(task,"信息","任务已创建" if mode=="real" else "安全演示任务已启动：只生成模拟遥测数据");STATE["tasks"].insert(0,task);persist();threading.Thread(target=fio_runner if mode=="real" else demo_runner,args=(task["id"],),daemon=True).start();return self.send_json(task,201)
                if path.startswith("/api/tasks/") and path.endswith("/stop"):
                    task=next((x for x in STATE["tasks"] if x["id"]==path.split("/")[3]),None)
                    if not task:return self.send_json({"error":"任务不存在"},404)
                    if task["status"]=="运行中":
                        process=PROCESSES.get(task["id"]);
                        if process: process.terminate()
                        task["status"]="已停止";task["ended_at"]=now();task["result"]="人工终止";event(task,"警告","任务由操作员停止");persist()
                    return self.send_json(task)
            return self.send_json({"error":"接口不存在"},404)
        except (json.JSONDecodeError,KeyError,ValueError) as exc:return self.send_json({"error":f"请求无效：{exc}"},400)
if __name__=="__main__":
    print("SSD PressureTest: http://127.0.0.1:8080"); ThreadingHTTPServer(("127.0.0.1",int(os.getenv("PORT","8080"))),Handler).serve_forever()

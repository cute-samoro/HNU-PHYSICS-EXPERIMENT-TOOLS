import json
import math
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "http://10.62.106.112"
REQUEST_TIMEOUT = 20
COOKIE_FIELDS = (
    "ASP.NET_SessionId",
    "COOKIES_KEY_USERNAME",
    "CurrentCourseMenu",
    "fzsy",
)
SECRET_COOKIE_FIELDS = {"ASP.NET_SessionId", "fzsy"}


def parse_cookie(raw_cookie):
    cookies = []
    for item in raw_cookie.replace("\n", ";").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if name:
            cookies.append(f"{name}={value.strip()}")
    if not cookies:
        raise ValueError("Cookie 格式为空，请粘贴 name=value; name2=value2 格式的 Cookie。")
    return "; ".join(cookies)


def build_cookie(values):
    parts = []
    for name in COOKIE_FIELDS:
        value = str(values.get(name, "")).strip()
        if value:
            parts.append(f"{name}={value}")
    if not parts:
        raise ValueError("请至少填写一个 Cookie 值。")
    return "; ".join(parts)


class EduClient:
    def __init__(self, raw_cookie):
        self.cookie = parse_cookie(raw_cookie)
        self.token = ""
        self.user_id = ""
        self.user_name = ""
        self.semester_id = ""
        self.semester_name = ""

    def request(self, path, method="GET", data=None):
        body = None
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Cookie": self.cookie,
            "User-Agent": "ExperimentClient/1.0",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.token:
            headers["Authorization"] = self.token
        if data is not None:
            body = urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        request = Request(BASE_URL + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read().decode("utf-8-sig", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"服务器返回 HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"无法连接教务系统: {exc.reason}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            if "Object moved" in raw or "登录" in raw:
                raise RuntimeError("Cookie 已失效或未授权，请重新复制当前浏览器 Cookie。") from exc
            raise RuntimeError(f"服务器返回了无法识别的数据: {raw[:300]}") from exc

    def login(self):
        info_result = self.request("/BaseInfo/Login/LoadUserInfo_novalidation", "POST", {})
        if not info_result.get("IsSuccess"):
            raise RuntimeError(info_result.get("ErrorInfo") or "读取登录信息失败")
        info = json.loads(info_result.get("Data") or "{}")
        self.user_id = str(info.get("UserID") or "")
        self.user_name = str(info.get("UserName") or "")
        self.token = str(info.get("Token") or self.user_id)
        self.semester_id = str(info.get("SemesterID") or "")
        self.semester_name = str(info.get("SemesterName") or "")
        if not self.user_id:
            raise RuntimeError("Cookie 会话有效，但没有读取到学生账号。")

    def selected_courses(self):
        result = self.request(
            "/XPK/StuCourseElective/LoadTableInfo2",
            "POST",
            {"page": 1, "rows": 100},
        )
        if result.get("Success") and result.get("RTNCode") == -1:
            raise RuntimeError("课程接口拒绝访问，请刷新 Cookie。")
        return result.get("rows") or []

    def _load_lab_rows(self, course_id, include_full=True, is_lab=1):
        base_data = {
            "courseID": course_id,
            "userID": self.user_id,
            "isBatch": 0,
            "SemesterID": self.semester_id,
            "IsLab": is_lab,
            "IsFull": 1 if include_full else 0,
            "page": 1,
            "rows": 100,
        }
        first = self.request(
            "/XPK/StuCourseElective/LoadUnuseLabCourses", "POST", base_data
        )
        rows = list(first.get("rows") or [])
        total = int(first.get("total") or len(rows))
        pages = max(1, math.ceil(total / 100))
        for page in range(2, pages + 1):
            page_data = dict(base_data)
            page_data["page"] = page
            response = self.request(
                "/XPK/StuCourseElective/LoadUnuseLabCourses", "POST", page_data
            )
            rows.extend(response.get("rows") or [])
        return rows

    def available_labs(self, course_id, include_full=True, include_virtual=False):
        rows = self._load_lab_rows(course_id, include_full, is_lab=1)
        if include_virtual:
            # On this platform IsLab=0 is the virtual-simulation branch.
            virtual_rows = self._load_lab_rows(course_id, include_full, is_lab=0)
            rows.extend(row for row in virtual_rows if ExperimentApp.is_virtual_lab(row))
        return self._deduplicate_lab_rows(rows)

    def _load_used_lab_rows(self, course_id, is_lab=None):
        data = {
            "courseID": course_id,
            "StuIds": self.user_id,
            "isBatch": 0,
            "SemesterID": self.semester_id,
            "page": 1,
            "rows": 100,
        }
        if is_lab is not None:
            data["IsLab"] = is_lab
        return self.request(
            "/XPK/StuCourseElective/LoadUsedLabCourses",
            "POST",
            data,
        ).get("rows") or []

    def used_labs(self, course_id, include_virtual=False):
        rows = self._load_used_lab_rows(course_id)
        if include_virtual:
            virtual_rows = self._load_used_lab_rows(course_id, is_lab=0)
            rows.extend(row for row in virtual_rows if ExperimentApp.is_virtual_lab(row))
        return self._deduplicate_lab_rows(rows)

    @staticmethod
    def _deduplicate_lab_rows(rows):
        unique_rows = []
        seen = set()
        for row in rows:
            key = row.get("LabClassNo") or row.get("LabID") or row.get("ID")
            if key is None:
                key = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
            if key not in seen:
                seen.add(key)
                unique_rows.append(row)
        return unique_rows

    def choose_lab(self, row):
        choose = {
            "WeekId": row.get("WeekID"),
            "Weeks": row.get("Weeks"),
            "LabID": row.get("LabID"),
            "IsLab": row.get("IsLab", 0 if ExperimentApp.is_virtual_lab(row) else 1),
            "TimePartID": row.get("TimePartID"),
            "Capacity": row.get("Capacity"),
            "IsQuried": row.get("Isquried"),
            "ClassDate": row.get("ClassDate"),
            "StartTime": row.get("StartTime"),
            "EndTime": row.get("EndTime"),
            "OpenTime": f"{row.get('StartTime', '')}:00",
            "ModuleID": row.get("ModuleID"),
            "CourseID": row.get("CourseID"),
            "StudentID": self.user_id,
            "TeacherID": row.get("TeacherID"),
            "SemesterID": self.semester_id,
            "LabGroupID": row.get("LabGroupID"),
            "LabStatusID": row.get("LabStatusID"),
            "LabClassNo": row.get("LabClassNo"),
            "LabName": row.get("LabName"),
        }
        return self.request(
            "/XPK/StuCourseElective/UseCoursesLab",
            "POST",
            {
                "ObjectIDs": json.dumps([choose], ensure_ascii=False),
                "isBatch": 0,
                "stuids": self.user_id,
            },
        )


class ExperimentApp:
    columns = (
        ("LabName", "实验名称", 260),
        ("ClassDate", "日期", 105),
        ("WeekName", "星期", 70),
        ("TimePartName", "时段", 70),
        ("TeacherName", "教师", 90),
        ("ClassRoom", "教室", 100),
        ("ElectivedNum", "人数", 65),
        ("LearningHours", "学时", 55),
        ("LabStatusID", "状态", 55),
    )

    def __init__(self, root):
        self.root = root
        self.root.title("实验选修客户端")
        self.root.geometry("1180x700")
        self.root.minsize(920, 560)
        self.client = None
        self.courses = []
        self.lab_rows = []
        self.used_rows = []
        self.visible_lab_count = 0
        self.busy = False
        self.auto_retry_active = False
        self.retry_stop_event = threading.Event()
        self.retry_generation = 0
        self._build_ui()

    def _build_ui(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        auth = ttk.LabelFrame(root, text="登录")
        auth.grid(row=0, column=0, padx=10, pady=(10, 6), sticky="ew")
        auth.columnconfigure(1, weight=1)
        ttk.Label(auth, text="Cookie 名称").grid(row=0, column=0, padx=8, pady=(6, 3), sticky="w")
        ttk.Label(auth, text="Cookie 值").grid(row=0, column=1, padx=4, pady=(6, 3), sticky="w")
        self.cookie_vars = {}
        self.cookie_entries = {}
        for row, name in enumerate(COOKIE_FIELDS, start=1):
            ttk.Label(auth, text=name).grid(row=row, column=0, padx=8, pady=3, sticky="w")
            variable = tk.StringVar()
            entry = ttk.Entry(
                auth,
                textvariable=variable,
                show="*" if name in SECRET_COOKIE_FIELDS else "",
            )
            entry.grid(row=row, column=1, padx=4, pady=3, sticky="ew")
            self.cookie_vars[name] = variable
            self.cookie_entries[name] = entry
        self.login_button = ttk.Button(auth, text="登录并抓取课程", command=self.load_courses)
        self.login_button.grid(row=1, column=2, rowspan=len(COOKIE_FIELDS), padx=8, pady=3, sticky="ns")
        self.show_cookie_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            auth,
            text="显示 Cookie",
            variable=self.show_cookie_var,
            command=self.toggle_cookie_visibility,
        ).grid(row=5, column=2, padx=8, pady=(3, 6))
        ttk.Label(auth, text=f"服务器: {BASE_URL}").grid(row=5, column=0, columnspan=2, padx=8, pady=(3, 6), sticky="w")

        controls = ttk.Frame(root)
        controls.grid(row=1, column=0, padx=10, pady=4, sticky="ew")
        controls.columnconfigure(3, weight=1)
        ttk.Label(controls, text="课程").grid(row=0, column=0, padx=(0, 5))
        self.course_var = tk.StringVar()
        self.course_box = ttk.Combobox(controls, textvariable=self.course_var, state="readonly", width=35)
        self.course_box.grid(row=0, column=1, padx=5)
        self.course_box.bind("<<ComboboxSelected>>", lambda _event: self.load_labs())
        self.refresh_button = ttk.Button(controls, text="刷新实验", command=self.load_labs, state="disabled")
        self.refresh_button.grid(row=0, column=2, padx=5)
        ttk.Label(controls, text="筛选").grid(row=0, column=4, padx=(16, 5))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_args: self.render_labs())
        ttk.Entry(controls, textvariable=self.filter_var, width=28).grid(row=0, column=5, padx=5)
        self.include_full_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            controls,
            text="显示已满课堂",
            variable=self.include_full_var,
            command=self.load_labs,
        ).grid(row=0, column=6, padx=5)
        # Match the website: virtual simulation classes are opt-in.
        self.include_virtual_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            controls,
            text="显示虚拟仿真",
            variable=self.include_virtual_var,
            command=self.load_labs,
        ).grid(row=0, column=7, padx=5)

        main = ttk.Frame(root)
        main.grid(row=2, column=0, padx=10, pady=4, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(main)
        self.notebook.grid(row=0, column=0, columnspan=2, sticky="nsew")
        available_tab = ttk.Frame(self.notebook)
        selected_tab = ttk.Frame(self.notebook)
        available_tab.columnconfigure(0, weight=1)
        available_tab.rowconfigure(0, weight=1)
        selected_tab.columnconfigure(0, weight=1)
        selected_tab.rowconfigure(0, weight=1)
        self.notebook.add(available_tab, text="可选实验")
        self.notebook.add(selected_tab, text="已选实验")
        self.tree = self.create_tree(available_tab)
        self.selected_tree = self.create_tree(selected_tab)
        self.tree.bind("<Double-1>", lambda _event: self.choose_selected())

        actions = ttk.Frame(root)
        actions.grid(row=3, column=0, padx=10, pady=4, sticky="ew")
        self.choose_button = ttk.Button(actions, text="选择当前实验", command=self.choose_selected, state="disabled")
        self.choose_button.pack(side="left")
        self.auto_retry_button = ttk.Button(
            actions,
            text="自动重试选修",
            command=self.start_auto_retry,
            state="disabled",
        )
        self.auto_retry_button.pack(side="left", padx=(8, 0))
        ttk.Label(actions, text="间隔").pack(side="left", padx=(14, 3))
        self.retry_interval_var = tk.StringVar(value="3")
        self.retry_interval_box = ttk.Spinbox(
            actions,
            from_=2,
            to=60,
            increment=1,
            textvariable=self.retry_interval_var,
            width=5,
        )
        self.retry_interval_box.pack(side="left")
        ttk.Label(actions, text="秒").pack(side="left", padx=(3, 0))
        self.stop_retry_button = ttk.Button(
            actions,
            text="停止重试",
            command=self.stop_auto_retry,
            state="disabled",
        )
        self.stop_retry_button.pack(side="left", padx=(8, 0))
        self.status_var = tk.StringVar(value="请输入当前浏览器 Cookie。Cookie 仅保存在本次运行内存中。")
        ttk.Label(actions, textvariable=self.status_var).pack(side="left", padx=12)

        self.log = ScrolledText(root, height=6, state="disabled", wrap="word")
        self.log.grid(row=4, column=0, padx=10, pady=(2, 10), sticky="ew")

    def create_tree(self, parent):
        tree = ttk.Treeview(parent, columns=[c[0] for c in self.columns], show="headings", selectmode="browse")
        for key, title, width in self.columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=45, anchor="center")
        tree.column("LabName", anchor="w")
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        return tree

    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def toggle_cookie_visibility(self):
        show = "" if self.show_cookie_var.get() else "*"
        for name in SECRET_COOKIE_FIELDS:
            self.cookie_entries[name].configure(show=show)

    def set_busy(self, busy, message=None):
        self.busy = busy
        self.update_action_states()
        if message:
            self.status_var.set(message)

    def update_action_states(self):
        self.login_button.configure(state="disabled" if self.busy or self.auto_retry_active else "normal")
        self.refresh_button.configure(
            state="disabled" if self.busy or self.auto_retry_active or not self.client else "normal"
        )
        has_labs = self.visible_lab_count > 0
        self.choose_button.configure(
            state="disabled" if self.busy or self.auto_retry_active or not has_labs else "normal"
        )
        self.auto_retry_button.configure(
            state="disabled" if self.busy or self.auto_retry_active or not has_labs else "normal"
        )
        self.retry_interval_box.configure(state="disabled" if self.auto_retry_active else "normal")
        self.stop_retry_button.configure(state="normal" if self.auto_retry_active else "disabled")

    def run_async(self, work, success, failure=None):
        if self.busy:
            return
        self.set_busy(True, "正在读取教务系统...")

        def worker():
            try:
                result = work()
            except Exception as exc:
                self.root.after(0, lambda: (self.set_busy(False), (failure or self.show_error)(str(exc))))
                return
            self.root.after(0, lambda: (self.set_busy(False), success(result)))

        threading.Thread(target=worker, daemon=True).start()

    def load_courses(self):
        try:
            cookie_header = build_cookie(
                {name: variable.get() for name, variable in self.cookie_vars.items()}
            )
        except ValueError as exc:
            self.show_error(str(exc))
            return

        def work():
            client = EduClient(cookie_header)
            client.login()
            return client, client.selected_courses()

        self.run_async(work, self.courses_loaded)

    def courses_loaded(self, result):
        self.client, self.courses = result
        values = [f"{row.get('CourseName', '')}  (课程ID {row.get('CourseID', '')})" for row in self.courses]
        self.course_box.configure(values=values)
        if values:
            self.course_box.current(0)
            self.refresh_button.configure(state="normal")
            self.write_log(f"登录成功：{self.client.user_id}，学期 {self.client.semester_name or self.client.semester_id}。")
            self.write_log(f"已选课程 {len(values)} 门，正在读取第一门课程的实验列表。")
            self.load_labs()
        else:
            self.refresh_button.configure(state="disabled")
            self.write_log("登录成功，但当前没有已选课程。")
            self.status_var.set("没有读取到已选课程。")

    def current_course(self):
        index = self.course_box.current()
        return self.courses[index] if index >= 0 and index < len(self.courses) else None

    def load_labs(self):
        course = self.current_course()
        if not course or not self.client:
            return
        course_id = course.get("CourseID")
        include_full = self.include_full_var.get()
        include_virtual = self.include_virtual_var.get()

        def work():
            available = self.client.available_labs(course_id, include_full, include_virtual)
            used = self.client.used_labs(course_id, True)
            return available, used

        self.run_async(work, self.labs_loaded)

    def labs_loaded(self, result):
        available, used = result
        self.lab_rows = available
        self.used_rows = used
        self.render_labs()
        self.render_selected()
        course = self.current_course() or {}
        virtual_count = sum(1 for row in available if self.is_virtual_lab(row))
        self.write_log(
            f"{course.get('CourseName', '')}: 可选实验 {len(available)} 条（虚拟/仿真 {virtual_count} 条），"
            f"已选实验 {len(used)} 条。"
        )
        self.status_var.set(f"已更新 {len(available)} 条实验；已选 {len(used)} 条。")

    def row_values(self, row):
        values = []
        for key, _title, _width in self.columns:
            value = row.get(key, "")
            if key == "LabName" and self.is_virtual_lab(row):
                value = f"[虚拟] {value}"
            elif key == "ClassDate":
                value = str(value).split(" ")[0].replace("/", "-")
            elif key == "ElectivedNum":
                value = f"{row.get('ElectivedNum', '')}/{row.get('Capacity', '')}"
            elif key == "LabStatusID":
                value = {"0": "正常", "1": "取消", "2": "允许补选", "3": "已调课", "4": "调课的"}.get(str(value), str(value))
            values.append(value)
        return values

    def render_labs(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        keyword = self.filter_var.get().strip().lower()
        shown = 0
        for index, row in enumerate(self.lab_rows):
            if not self.include_virtual_var.get() and self.is_virtual_lab(row):
                continue
            values = self.row_values(row)
            haystack = " ".join(str(value) for value in values).lower()
            if keyword and keyword not in haystack:
                continue
            tag = "virtual" if self.is_virtual_lab(row) else ("full" if self.is_full(row) else "open")
            self.tree.insert("", "end", iid=str(index), values=values, tags=(tag,))
            shown += 1
        self.tree.tag_configure("full", foreground="#9a3d3d")
        self.tree.tag_configure("open", foreground="#1b5e20")
        self.tree.tag_configure("virtual", foreground="#365f91")
        self.visible_lab_count = shown
        self.update_action_states()

    def render_selected(self):
        for item in self.selected_tree.get_children():
            self.selected_tree.delete(item)
        for index, row in enumerate(self.used_rows):
            self.selected_tree.insert("", "end", iid=str(index), values=self.row_values(row))

    @staticmethod
    def is_full(row):
        try:
            return int(row.get("ElectivedNum") or 0) >= int(row.get("Capacity") or 0)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def is_virtual_lab(row):
        truthy_fields = (
            "IsVirtual",
            "IsVirtualLab",
            "IsVirtualExperiment",
            "VirtualLab",
            "IsSimulation",
        )
        for field in truthy_fields:
            value = str(row.get(field, "")).strip().lower()
            if value in {"1", "true", "yes", "virtual", "simulation", "虚拟", "仿真"}:
                return True

        type_fields = (
            "LabType",
            "LabTypeName",
            "ExperimentType",
            "ExperimentTypeName",
            "LabCategory",
            "LabCategoryName",
            "LabMode",
            "LabModeName",
        )
        for field in type_fields:
            value = str(row.get(field, "")).lower()
            if any(word in value for word in ("虚拟", "仿真", "virtual", "simulation")):
                return True

        name = " ".join(
            str(row.get(field, ""))
            for field in (
                "ClassRoom",
                "Classroom",
                "ClassRoomName",
                "RoomName",
                "LabName",
                "CourseName",
                "ModuleName",
            )
        ).lower()
        return any(word in name for word in ("虚拟", "仿真", "virtual", "simulation"))

    def selected_row(self):
        selection = self.tree.selection()
        if not selection:
            return None
        return self.lab_rows[int(selection[0])]

    def choose_selected(self):
        row = self.selected_row()
        if not row or not self.client:
            self.show_error("请先选择一条实验。")
            return
        full_warning = "\n注意：该课堂当前显示为已满。" if self.is_full(row) else ""
        details = (
            f"课程：{row.get('CourseName', '')}\n"
            f"实验：{row.get('LabName', '')}\n"
            f"类型：{'虚拟/仿真实验' if self.is_virtual_lab(row) else '常规实验'}\n"
            f"时间：第{row.get('Weeks', '')}周 {row.get('WeekName', '')}"
            f" {row.get('TimePartName', '')} {row.get('ClassDate', '').split(' ')[0]} {row.get('StartTime', '')}\n"
            f"教师/教室：{row.get('TeacherName', '')} / {row.get('ClassRoom', '')}\n"
            f"人数：{row.get('ElectivedNum', '')}/{row.get('Capacity', '')}"
            f"{full_warning}\n\n"
            "确认后会立即向教务系统发送选修请求，并修改你的选课记录。"
        )
        if not messagebox.askyesno("确认提交选课", details, icon="warning"):
            return

        def work():
            return self.client.choose_lab(row)

        self.run_async(work, self.choose_result)

    def start_auto_retry(self):
        row = self.selected_row()
        if not row or not self.client:
            self.show_error("请先在可选实验列表中选择一条实验。")
            return
        if self.is_full(row):
            self.show_error("该课堂当前已满，无需继续重试。")
            return
        try:
            interval = float(self.retry_interval_var.get())
        except ValueError:
            self.show_error("重试间隔必须是数字。")
            return
        if interval < 2:
            self.show_error("为避免频繁请求，重试间隔不能小于 2 秒。")
            return

        details = (
            f"实验：{row.get('LabName', '')}\n"
            f"类型：{'虚拟/仿真实验' if self.is_virtual_lab(row) else '常规实验'}\n"
            f"时间：{row.get('ClassDate', '').split(' ')[0]} {row.get('TimePartName', '')}\n"
            f"教师/教室：{row.get('TeacherName', '')} / {row.get('ClassRoom', '')}\n"
            f"当前人数：{row.get('ElectivedNum', '')}/{row.get('Capacity', '')}\n\n"
            f"客户端将每 {interval:g} 秒提交一次选修请求，直到服务器确认成功、课堂已满，"
            "或你点击“停止重试”。确认后立即开始。"
        )
        if not messagebox.askyesno("确认自动重试", details, icon="warning"):
            return

        self.auto_retry_active = True
        self.retry_generation += 1
        generation = self.retry_generation
        stop_event = threading.Event()
        self.retry_stop_event = stop_event
        self.update_action_states()
        self.status_var.set("自动重试已开始。")
        self.write_log(f"开始自动重试：{row.get('LabName', '')}，间隔 {interval:g} 秒。")

        def work():
            attempts = 0
            while not stop_event.is_set():
                attempts += 1
                try:
                    result = self.client.choose_lab(row)
                    if result.get("IsSuccess"):
                        self.root.after(
                            0,
                            lambda attempts=attempts, generation=generation: self.finish_auto_retry(
                                f"服务器已接受选修请求，共尝试 {attempts} 次。", True, generation
                            ),
                        )
                        return

                    used = self.client.used_labs(
                        row.get("CourseID"), self.is_virtual_lab(row)
                    )
                    if self.is_lab_selected(row, used):
                        self.root.after(
                            0,
                            lambda attempts=attempts, generation=generation: self.finish_auto_retry(
                                f"已检测到实验选修成功，共尝试 {attempts} 次。", True, generation
                            ),
                        )
                        return

                    error = self.response_error(result)
                    if self.response_says_full(result) or self.is_full(row):
                        self.root.after(
                            0,
                            lambda attempts=attempts, error=error, generation=generation: self.finish_auto_retry(
                                f"课堂已满，自动重试停止（第 {attempts} 次：{error}）。", True, generation
                            ),
                        )
                        return
                    self.root.after(
                        0,
                        lambda attempts=attempts, error=error, generation=generation: self.retry_attempt_update(
                            attempts, error, generation
                        ),
                    )
                except Exception as exc:
                    self.root.after(
                        0,
                        lambda exc=exc, generation=generation: self.finish_auto_retry(
                            f"请求出错，自动重试停止：{exc}", False, generation
                        ),
                    )
                    return

                if stop_event.wait(interval):
                    return

        threading.Thread(target=work, daemon=True).start()

    def stop_auto_retry(self):
        if not self.auto_retry_active:
            return
        self.retry_stop_event.set()
        self.retry_generation += 1
        self.auto_retry_active = False
        self.update_action_states()
        self.status_var.set("自动重试已停止。")
        self.write_log("用户停止了自动重试。")

    def retry_attempt_update(self, attempts, error, generation):
        if not self.auto_retry_active or generation != self.retry_generation:
            return
        self.status_var.set(f"自动重试中，第 {attempts} 次未成功，等待下一次请求。")
        self.write_log(f"第 {attempts} 次未成功：{error}")

    def finish_auto_retry(self, message, refresh, generation):
        if not self.auto_retry_active or generation != self.retry_generation:
            return
        self.retry_stop_event.set()
        self.auto_retry_active = False
        self.update_action_states()
        self.status_var.set(message)
        self.write_log(message)
        if refresh and self.client:
            self.load_labs()

    @staticmethod
    def response_error(result):
        for key in ("ErrorInfo", "Message", "msg", "Msg", "Data"):
            value = result.get(key)
            if value:
                return str(value)
        return "服务器未接受本次请求"

    @classmethod
    def response_says_full(cls, result):
        text = cls.response_error(result).lower()
        return any(word in text for word in ("已满", "满员", "容量", "full", "人数已达"))

    @staticmethod
    def is_lab_selected(row, used_rows):
        for used in used_rows:
            if row.get("LabClassNo") and row.get("LabClassNo") == used.get("LabClassNo"):
                return True
            if row.get("LabID") and row.get("LabID") == used.get("LabID"):
                return True
        return False

    def choose_result(self, result):
        if result.get("IsSuccess"):
            self.write_log("选修请求成功：服务器返回 IsSuccess=true。")
            self.status_var.set("选修成功，正在刷新实验列表。")
            self.load_labs()
        else:
            error = result.get("ErrorInfo") or result.get("Data") or "服务器未接受选修请求。"
            self.write_log(f"选修请求未成功：{error}")
            self.show_error(str(error))

    def show_error(self, message):
        self.status_var.set("操作失败。")
        self.write_log(f"错误：{message}")
        messagebox.showerror("操作失败", message)


if __name__ == "__main__":
    app_root = tk.Tk()
    ExperimentApp(app_root)
    app_root.mainloop()

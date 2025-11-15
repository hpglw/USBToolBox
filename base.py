import binascii
import copy
import json
import platform
import plistlib
import shutil
import subprocess
import textwrap
from enum import Enum
from operator import itemgetter
from pathlib import Path

from termcolor2 import c as color

from Scripts import shared, utils



class Colors(Enum):
    BLUE = "\u001b[36;1m"
    RED = "\u001b[31;1m"
    GREEN = "\u001b[32;1m"
    RESET = "\u001b[0m"


def hexswap(input_hex: str):
    hex_pairs = [input_hex[i : i + 2] for i in range(0, len(input_hex), 2)]
    hex_rev = hex_pairs[::-1]
    hex_str = "".join(["".join(x) for x in hex_rev])
    return hex_str.upper()


def read_property(input_value: bytes, places: int):
    return binascii.b2a_hex(input_value).decode()[:-places]


class BaseUSBMap:
    def __init__(self):
        self.utils = utils.Utils(f"USBToolBox {shared.VERSION}".strip())
        self.controllers = None
        self.json_path = shared.current_dir / Path("usb.json")
        self.settings_path = shared.current_dir / Path("settings.json")

        self.settings = (
            json.load(self.settings_path.open()) if self.settings_path.exists() else {"show_friendly_types": True, "use_native": False, "use_legacy_native": False, "add_comments_to_map": True, "auto_bind_companions": True}
        )
        self.controllers_historical = json.load(self.json_path.open("r")) if self.json_path.exists() else None

        self.monu()

    @staticmethod
    def is_same_controller(controller_1, controller_2):
        common_keys: set[str] = set(i for i in controller_1["identifiers"] if controller_1["identifiers"][i]) & set(i for i in controller_2["identifiers"] if controller_2["identifiers"][i])
        if not common_keys:
            # No way to tell
            return False
        for key in common_keys:
            if key in ["instance_id", "location_id"]:
                # With Thunderbolt controllers, on hotplugs the instance ID will change, but everything else will be the same.
                # On macOS, location IDs can change all the time.
                continue
            elif len(common_keys) == 1 and key in ["pci_revision"]:
                # Don't match solely by pci_revision.
                return False
            elif key == "location_paths":
                # Some firmwares have broken ACPI where two or more devices have the same parent and same address, and Windows does not always show all
                # Evident with Thunderbolt controllers
                # We will be satisified if they have at least 1 in common
                if not set(controller_1["identifiers"]["location_paths"]) & set(controller_2["identifiers"]["location_paths"]):
                    return False
            else:
                if not (controller_1["identifiers"][key] is not None and controller_2["identifiers"][key] is not None and controller_1["identifiers"][key] == controller_2["identifiers"][key]):
                    return False
        return True

    @staticmethod
    def get_controller_from_list(original, controller_list):
        for controller in controller_list:
            if BaseUSBMap.is_same_controller(original, controller):
                return controller
        return None

    @staticmethod
    def merge_properties(old, new):
        if not new:
            return old
        if not old:
            return new
        if isinstance(old, list):
            retval = list(old)
            retval.extend(set(new) - set(old))
            return retval
        elif isinstance(old, dict):
            retval = dict(old)
            for key in new:
                retval[key] = BaseUSBMap.merge_properties(retval.get(key), new[key])
            return retval
        else:
            return new

    @staticmethod
    def merge_controllers(base: list, new: list):
        for controller in new:
            base_controller = BaseUSBMap.get_controller_from_list(controller, base)
            if not base_controller:
                base.append(controller)
                # Don't need to merge properties because there's no base controller
                continue

            for key in set(controller.keys()) - set(["ports"]):  # Leave merging ports to merge_ports
                base_controller[key] = BaseUSBMap.merge_properties(base_controller.get(key), controller[key])

        BaseUSBMap.merge_ports(base, new)

    @staticmethod
    def merge_ports(base: list, new: list):
        for controller in new:
            base_controller = BaseUSBMap.get_controller_from_list(controller, base)
            for port in controller["ports"]:
                base_port = ([p for p in base_controller["ports"] if p["index"] == port["index"]] or [None])[0]
                if not base_port:
                    base_controller["ports"].append(copy.deepcopy(port))
                    # Don't need to merge properties because there's no base port
                    continue

                for key in set(port.keys()) - set(["devices"]):  # Leave merging devices to merge_devices
                    base_port[key] = BaseUSBMap.merge_properties(base_port.get(key), port[key])
            base_controller["ports"].sort(key=itemgetter("index"))
        BaseUSBMap.merge_devices(base, new)

    @staticmethod
    def recursive_merge_devices(base: list, new: list):
        for i in new:
            if not i or i in base:
                continue
            elif isinstance(i, str):
                base.append(i)
            elif i.get("error"):
                continue
            elif i["name"] not in [hub.get("name") for hub in base if isinstance(hub, dict)]:
                base.append(i)
            else:
                old_hub = [hub for hub in base if isinstance(hub, dict) and hub.get("name") == i["name"]][0]
                BaseUSBMap.recursive_merge_devices(old_hub["devices"], i["devices"])
        for i in list(base):
            if not i or i.get("error"):
                base.remove(i)

    @staticmethod
    def merge_devices(base: list, new: list):
        for controller in base:
            new_controller = BaseUSBMap.get_controller_from_list(controller, new)
            if new_controller:
                for port in controller["ports"]:
                    BaseUSBMap.recursive_merge_devices(port["devices"], new_controller["ports"][controller["ports"].index(port)]["devices"])

    def get_controllers(self):
        raise NotImplementedError

    def update_devices(self):
        raise NotImplementedError

    def controller_to_str(self, controller):
        return f"{controller['name']} | {shared.USBControllerTypes(controller['class'])}"

    def port_to_str(self, port):
        if port["type"] is not None:
            port_type = shared.USBPhysicalPortTypes(port["type"]) if self.settings["show_friendly_types"] else shared.USBPhysicalPortTypes(port["type"]).value
        elif port["guessed"] is not None:
            port_type = (str(shared.USBPhysicalPortTypes(port["guessed"])) if self.settings["show_friendly_types"] else str(shared.USBPhysicalPortTypes(port["guessed"]).value)) + " (猜测值)"
        else:
            port_type = "未知"

        return f"{port['name']} | {shared.USBDeviceSpeeds(port['class'])} | " + (str(port_type) if self.settings["show_friendly_types"] else f"类型 {port_type}")

    def print_controllers(self, controllers, colored=False):
        if not controllers:
            print("内容为空。")
            return
        for controller in controllers:
            if colored:
                print(color(self.controller_to_str(controller) + f" | {len(controller['ports'])}个端口"))
            else:
                print(self.controller_to_str(controller) + f" | {len(controller['ports'])}个端口")
            for port in controller["ports"]:
                if not colored:
                    print("  " + self.port_to_str(port))
                elif port["devices"]:
                    print("  " + color(self.port_to_str(port)).green.bold)
                elif (
                    self.get_controller_from_list(controller, self.controllers_historical)
                    and [i for i in self.get_controller_from_list(controller, self.controllers_historical)["ports"] if i["index"] == port["index"]][0]["devices"]
                ):
                    print("  " + color(self.port_to_str(port)).cyan.bold)
                else:
                    print("  " + self.port_to_str(port))

                if port["comment"]:
                    print("  " + port["comment"])
                for device in port["devices"]:
                    self.print_devices(device)

    def print_devices(self, device, indentation="    "):
        if not device:
            device = "枚举中..."
        if isinstance(device, str):
            print(f"{indentation}- {device}")
        elif device.get("error", False):
            print(f"{indentation}- {device['error'] if isinstance(device['error'], str) else '连接到此端口的设备出错。'} 请拔下或连接一个不同的设备。")
        else:
            print(f"{indentation}- {device['name'].strip()} - 运行速度: {shared.USBDeviceSpeeds(device['speed'])}")
            for i in device["devices"]:
                self.print_devices(i, indentation + "  ")

    def discover_ports(self):
        self.utils.head("端口发现")
        print()
        dont_refresh = False
        if not self.controllers:
            print("\n正在获取控制器...")
            self.get_controllers()
            dont_refresh = True
        while True:
            # os.system("cls" if os.name == "nt" else "clear")
            self.utils.head("端口发现")
            print()
            if dont_refresh:
                dont_refresh = False
            else:
                self.update_devices()

            self.print_controllers(self.controllers, colored=True)

            self.dump_historical()
            print("\nB.  返回\n")
            do_quit = self.utils.grab("等待5秒后将自动刷新: ", timeout=5)
            if str(do_quit).lower() == "b":
                break

    def print_historical(self):
        utils.TUIMenu("打印历史记录 (调试)", "请选择一个选项: ", in_between=lambda: self.print_controllers(self.controllers_historical), loop=True).start()

    def dump_historical(self):
        if self.controllers_historical:
            json.dump(self.controllers_historical, self.json_path.open("w"), indent=4, sort_keys=True)
        elif self.controllers_historical == []:
            self.remove_historical()

    def remove_historical(self):
        if self.controllers_historical or self.controllers_historical == []:
            self.controllers = None
            self.controllers_historical = None
            self.json_path.unlink()

    def dump_settings(self):
        json.dump(self.settings, self.settings_path.open("w"), indent=4, sort_keys=True)

    def on_quit(self):
        self.dump_historical()

    def print_types(self):
        in_between = [f"{i}: {i.value}" for i in shared.USBPhysicalPortTypes] + [
            "",
            textwrap.dedent(
                """\
            接口类型9和10的区别在于：如果您反转插头后，设备仍然连接到和之前相同的端口，那么它就带有切换开关 (类型9)。
            如果反转后连接到了不同的端口，那么它就没有切换开关 (类型10)。"""
            ),
            "",
            "更多信息和图片，请访问 https://github.com/USBToolBox/tool/blob/master/TYPES.md。",
        ]
        utils.TUIMenu("USB 接口类型", "请选择一个选项: ", in_between=in_between).start()

    def get_companion_port(self, port):
        if not port.get("companion_info"):
            return None
        companion_info = port["companion_info"]
        if not companion_info["hub"] or not companion_info["port"]:
            return None
        hub = [i for i in self.controllers_historical if i.get("hub_name") == companion_info["hub"]]
        if hub:
            port = [i for i in hub[0]["ports"] if i["index"] == companion_info["port"]]
            if port:
                return port[0]
        return None

    def select_ports(self):
        if not self.controllers_historical:
            utils.TUIMenu("选择端口并构建Kext", "请选择一个选项: ", in_between=["没有端口信息！请先使用端口发现模式。"], loop=True).start()
            return

        selection_index = 1
        by_port = []
        for controller in self.controllers_historical:
            controller["selected_count"] = 0
            for port in controller["ports"]:
                if "selected" not in port:
                    port["selected"] = bool(port["devices"])
                    port["selected"] = port["selected"] or (bool(self.get_companion_port(port)["devices"]) if self.get_companion_port(port) else False)
                controller["selected_count"] += 1 if port["selected"] else 0
                port["selection_index"] = selection_index
                selection_index += 1
                by_port.append(port)

        while True:
            self.dump_historical()
            for controller in self.controllers_historical:
                controller["selected_count"] = sum(1 if port["selected"] else 0 for port in controller["ports"])

            utils.header("选择端口并构建Kext")
            print()
            for controller in self.controllers_historical:
                port_count_str = f"{controller['selected_count']}/{len(controller['ports'])}"
                port_count_str = color(port_count_str).red if controller["selected_count"] > 15 else color(port_count_str).green
                print(self.controller_to_str(controller) + f" | {port_count_str} 个端口")
                for port in controller["ports"]:
                    port_info = f"[{'#' if port['selected'] else ' '}]  {port['selection_index']}.{(len(str(selection_index)) - len(str(port['selection_index'])) + 1) * ' ' }" + self.port_to_str(port)
                    companion = self.get_companion_port(port)
                    if companion:
                        port_info += f" | 伴生端口: {companion['selection_index']}"
                    if port["selected"]:
                        print(color(port_info).green.bold)
                    else:
                        print(port_info)
                    if port["comment"]:
                        print(
                            len(f"[{'#' if port['selected'] else ' '}]  {port['selection_index']}.{(len(str(selection_index)) - len(str(port['selection_index'])) + 1) * ' ' }") * " "
                            + color(port["comment"]).blue.bold
                        )
                    for device in port["devices"]:
                        self.print_devices(device, indentation="      " + len(str(selection_index)) * " " * 2)
                print()

            print(f"伴生端口绑定当前为 {color('开启').green if self.settings['auto_bind_companions'] else color('关闭').red} 状态。\n")

            output_kext = None
            if self.settings["use_native"] and self.settings["use_legacy_native"]:
                output_kext = "USBMapLegacy.kext"
            elif self.settings["use_native"]:
                output_kext = "USBMap.kext"
            else:
                output_kext = "UTBMap.kext (需要 USBToolBox.kext)"

            print(
                textwrap.dedent(
                    f"""\
                K. 构建 {output_kext}
                A. 全选
                N. 全部不选
                P. 启用所有已连接设备的端口
                D. 禁用所有空端口
                T. 显示类型说明

                B. 返回

                - 使用逗号分隔的列表来切换端口 (例如 1,2,3,4,5)
                - 使用公式 T:1,2,3,4,5:t 更改类型，其中t为类型编号
                - 使用公式 C:1:名称 设置自定义名称 - 名称 = None 可清除"""
                )
            )

            output = input("请选择一个选项: ")
            if not output:
                continue
            elif output.upper() == "B":
                break
            elif output.upper() == "K":
                if not self.validate_selections():
                    continue
                self.build_kext()
                continue
            elif output.upper() in ("N", "A"):
                for port in by_port:
                    port["selected"] = output.upper() == "A"
            elif output.upper() == "P":
                for port in by_port:
                    if port["devices"] or (self.get_companion_port(port)["devices"] if self.get_companion_port(port) else False):
                        port["selected"] = True
            elif output.upper() == "D":
                for port in by_port:
                    if not port["devices"] and not (self.get_companion_port(port)["devices"] if self.get_companion_port(port) else False):
                        port["selected"] = False
            elif output.upper() == "T":
                self.print_types()
                continue
            elif output[0].upper() == "T":
                # We should have a type
                if len(output.split(":")) != 3:
                    continue
                try:
                    port_nums, port_type = output.split(":")[1:]
                    port_nums = port_nums.replace(" ", "").split(",")
                    port_type = shared.USBPhysicalPortTypes(int(port_type))

                    for port_num in list(port_nums):
                        if port_num not in port_nums:
                            continue

                        port_num = int(port_num) - 1

                        if port_num not in range(len(by_port)):
                            continue

                        companion = self.get_companion_port(by_port[port_num])
                        if self.settings["auto_bind_companions"] and companion:
                            companion["type"] = port_type
                            if str(companion["selection_index"]) in port_nums:
                                port_nums.remove(str(companion["selection_index"]))
                        by_port[port_num]["type"] = port_type
                except ValueError:
                    continue
            elif output[0].upper() == "C":
                # We should have a new name
                if len(output.split(":")) < 2:
                    continue
                try:
                    port_nums = output.split(":")[1].replace(" ", "").split(",")
                    port_comment = output.split(":", 2)[2:]

                    for port_num in list(port_nums):
                        if port_num not in port_nums:
                            continue

                        port_num = int(port_num) - 1

                        if port_num not in range(len(by_port)):
                            continue

                        by_port[port_num]["comment"] = port_comment[0] if port_comment else None
                except ValueError:
                    continue
            else:
                try:
                    port_nums = output.replace(" ", "").split(",")

                    for port_num in list(port_nums):
                        if port_num not in port_nums:
                            continue

                        port_num = int(port_num) - 1

                        if port_num not in range(len(by_port)):
                            continue

                        companion = self.get_companion_port(by_port[port_num])
                        if self.settings["auto_bind_companions"] and companion:
                            companion["selected"] = not by_port[port_num]["selected"]
                            if str(companion["selection_index"]) in port_nums:
                                port_nums.remove(str(companion["selection_index"]))
                        by_port[port_num]["selected"] = not by_port[port_num]["selected"]
                except ValueError:
                    continue

    def print_errors(self, errors):
        if not errors:
            return True
        utils.TUIMenu("选择验证", "请选择一个选项: ", in_between=errors, loop=True).start()
        return False

    def validate_selections(self):
        errors = []
        if not any(any(p["selected"] for p in c["ports"]) for c in self.controllers_historical):
            utils.TUIMenu("选择验证", "请选择一个选项: ", in_between=["未选择任何端口！请选择一些端口。"], loop=True).start()
            return False

        for controller in self.controllers_historical:
            for port in controller["ports"]:
                if not port["selected"]:
                    continue
                if port["type"] is None and port["guessed"] is None:
                    errors.append(f"端口 {port['selection_index']} 缺少接口类型！")

        return self.print_errors(errors)

    def check_unique(self, identifier, predicate, controller):
        if predicate(controller):
            values = [identifier(c) for c in self.controllers_historical if predicate(c)]
            return values.count(identifier(controller)) == 1
        else:
            return False

    def choose_matching_key(self, controller):
        if "bus_number" in controller["identifiers"]:
            # M1 Macs
            return {"IOPropertyMatch": {"bus-number": binascii.a2b_hex(hexswap(hex(controller["identifiers"]["bus_number"])[2:].zfill(8)))}}

        elif not self.settings["use_native"] and self.check_unique(lambda c: c["identifiers"]["acpi_path"].rpartition(".")[2], lambda c: "acpi_path" in c["identifiers"], controller):
            # Unique ACPI name
            # Disable if using native because we don't know if it'll conflict
            # TODO: Check this maybe?
            shared.debug(f"Using ACPI path: {controller['identifiers']['acpi_path']}")
            return {"IONameMatch": controller["identifiers"]["acpi_path"].rpartition(".")[2]}

        elif "bdf" in controller["identifiers"]:
            # Use bus-device-function
            return {"IOPropertyMatch": {"pcidebug": ":".join([str(i) for i in controller["identifiers"]["bdf"]])}}

        elif self.check_unique(lambda c: c["identifiers"]["path"], lambda c: "path" in c["identifiers"], controller):
            # Use IORegistry path
            return {"IOPathMatch": controller["identifiers"]["path"]}

        elif self.check_unique(lambda c: c["identifiers"]["pci_id"], lambda c: "pci_id" in c["identifiers"], controller):
            # Use PCI ID
            pci_id: list[str] = controller["identifiers"]["pci_id"]
            return {"IOPCIPrimaryMatch": f"0x{pci_id[1]}{pci_id[0]}"} | ({"IOPCISecondaryMatch": f"0x{pci_id[3]}{pci_id[2]}"} if len(pci_id) > 2 else {})

        else:
            raise RuntimeError("No matching key available")

    def build_kext(self):
        empty_controllers = [c for c in self.controllers_historical if not any(p["selected"] for p in c["ports"])]
        response = None
        if empty_controllers:
            empty_menu = utils.TUIMenu(
                "选择验证",
                "请选择一个选项: ",
                in_between=["以下控制器没有任何启用的端口:", ""]
                + [controller["name"] for controller in empty_controllers]
                + ["请选择是忽略这些控制器并将其从配置中排除，还是禁用这些控制器上的所有端口。"],
                add_quit=False,
                return_number=True,
            )
            empty_menu.add_menu_option("忽略", key="I")
            empty_menu.add_menu_option("禁用", key="D")
            response = empty_menu.start()

        model_identifier = None
        if self.settings["use_native"]:
            if platform.system() == "Darwin":
                model_identifier = plistlib.loads(subprocess.run("system_profiler -detailLevel mini -xml SPHardwareDataType".split(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.strip())[
                    0
                ]["_items"][0]["machine_model"]
            else:
                model_menu = utils.TUIOnlyPrint(
                    "输入机型标识符",
                    "请输入机型标识符: ",
                    [
                        "您已选择使用原生类，因此会看到此提示。由于您不在macOS上，无法自动检测机型标识符。",
                        "请在下方输入目标系统的机型标识符。您可以在“系统信息”或使用命令 'system_profiler -detailLevel mini SPHardwareDataType' 找到它。",
                    ],
                ).start()
                model_identifier = model_menu.strip()

        ignore = response == "I"

        template = plistlib.load((shared.resource_dir / Path("Info.plist")).open("rb"))

        menu = utils.TUIMenu("正在构建 USBMap", "请选择一个选项: ")
        menu.head()
        print("正在生成 Info.plist...")
        for controller in self.controllers_historical:
            if not any(i["selected"] for i in controller["ports"]) and ignore:
                continue

            # FIXME: ensure unique
            if controller["identifiers"].get("acpi_path"):
                if self.check_unique(lambda c: c["identifiers"]["acpi_path"].rpartition(".")[2], lambda c: "acpi_path" in c["identifiers"], controller):
                    personality_name: str = controller["identifiers"]["acpi_path"].rpartition(".")[2]
                else:
                    personality_name: str = controller["identifiers"]["acpi_path"][1:]  # Strip leading \
            elif controller["identifiers"].get("bdf"):
                personality_name: str = ":".join([str(i) for i in controller["identifiers"]["bdf"]])
            else:
                personality_name: str = controller["name"]

            if self.settings["use_native"]:
                personality = {
                    "CFBundleIdentifier": "com.apple.driver." + ("AppleUSBMergeNub" if self.settings["use_legacy_native"] else "AppleUSBHostMergeProperties"),
                    "IOClass": ("AppleUSBMergeNub" if self.settings["use_legacy_native"] else "AppleUSBHostMergeProperties"),
                    "IOProviderClass": "AppleUSBHostController",
                    "IOParentMatch": self.choose_matching_key(controller),
                    "model": model_identifier,
                }

            else:
                personality = {
                    "CFBundleIdentifier": "com.dhinakg.USBToolBox.kext",
                    "IOClass": "USBToolBox",
                    "IOProviderClass": "IOPCIDevice",
                    "IOMatchCategory": "USBToolBox",
                } | self.choose_matching_key(
                    controller
                )  # type: ignore

            personality["IOProviderMergeProperties"] = {"ports": {}, "port-count": None}

            port_name_index = {}
            highest_index = 0

            for port in controller["ports"]:
                if not port["selected"]:
                    continue

                if port["index"] > highest_index:
                    highest_index = port["index"]

                if controller["class"] == shared.USBControllerTypes.XHCI and port["class"] == shared.USBDeviceSpeeds.SuperSpeed:
                    prefix = "SS"
                elif controller["class"] == shared.USBControllerTypes.XHCI and port["class"] == shared.USBDeviceSpeeds.HighSpeed:
                    prefix = "HS"
                else:
                    prefix = "PRT"

                port_index = port_name_index.setdefault(prefix, 1)
                port_name = prefix + str(port_index).zfill(4 - len(prefix))
                port_name_index[prefix] += 1

                personality["IOProviderMergeProperties"]["ports"][port_name] = {
                    "port": binascii.a2b_hex(hexswap(hex(port["index"])[2:].zfill(8))),
                    "UsbConnector": port["type"] or port["guessed"],
                }

                if self.settings["add_comments_to_map"] and port["comment"]:
                    personality["IOProviderMergeProperties"]["ports"][port_name]["#comment"] = port["comment"]

            personality["IOProviderMergeProperties"]["port-count"] = binascii.a2b_hex(hexswap(hex(highest_index)[2:].zfill(8)))

            template["IOKitPersonalities"][personality_name] = personality

        if not self.settings["use_native"]:
            template["OSBundleLibraries"] = {"com.dhinakg.USBToolBox.kext": "1.0.0"}

        output_kext = None
        if self.settings["use_native"] and self.settings["use_legacy_native"]:
            output_kext = "USBMapLegacy.kext"
        elif self.settings["use_native"]:
            output_kext = "USBMap.kext"
        else:
            output_kext = "UTBMap.kext"

        write_path = shared.current_dir / Path(output_kext)

        if write_path.exists():
            print("正在移除已存在的 kext...")
            shutil.rmtree(write_path)

        print("正在写入 kext 和 Info.plist...")
        (write_path / Path("Contents")).mkdir(parents=True)
        plistlib.dump(template, (write_path / Path("Contents/Info.plist")).open("wb"), sort_keys=True)
        print(f"完成。已保存至 {write_path.resolve()}.\n")
        menu.print_options()

        menu.select()
        return True

    def change_settings(self):
        def functionify(func):
            return lambda *args, **kwargs: lambda: func(*args, **kwargs)

        @functionify
        def color_status(name, variable):
            return f"{name}: {color('已启用').green if self.settings[variable] else color('已禁用').red}"

        @functionify
        def toggle_setting(variable):
            self.settings[variable] = not self.settings[variable]

        def combination(name, variable):
            return color_status(name, variable), toggle_setting(variable)

        menu = utils.TUIMenu("更改设置", "切换一个设置: ", loop=True)
        for i in [
            ["T", *combination("显示友好类型名称", "show_friendly_types"), ["显示友好的类型名称 (例如 'USB 3 Type A') 而不是数字。"]],
            ["N", *combination("使用原生类", "use_native"), ["使用苹果原生类 (AppleUSBHostMergeProperties) 而不是 USBToolBox kext。"]],
            ["L", *combination("使用旧版原生类 (需要启用'使用原生类')", "use_legacy_native"), ["为旧版 macOS 使用 AppleUSBMergeNub 而不是 AppleUSBHostMergeProperties。"]],
            ["A", *combination("在配置中添加注释", "add_comments_to_map"), ["将端口的自定义名称/注释添加到配置中。"]],
            [
                "C",
                *combination("绑定伴生端口", "auto_bind_companions"),
                ["将伴生端口绑定在一起。如果一个端口被启用/禁用/更改类型，另一个伴生端口也会受同样影响。"],
            ],
        ]:
            menu.add_menu_option(name=i[1], function=i[2], key=i[0], description=i[3] if len(i) == 4 else None)

        menu.start()
        self.dump_settings()

    def monu(self):
        response = None
        while not (response and response == utils.TUIMenu.EXIT_MENU):
            in_between = [("已保存数据: {}" + Colors.RESET.value).format(Colors.GREEN.value + "已加载" if self.json_path.exists() else (Colors.RED.value + "无"))]

            menu = utils.TUIMenu(f"USBToolBox {shared.VERSION}", "请选择一个选项: ", in_between=in_between, top_level=True)

            menu_options = [
                # ["H", "Print Historical", self.print_historical],
                ["D", "发现端口", self.discover_ports],
                ["S", "选择端口并构建Kext", self.select_ports],
                ["C", "更改设置", self.change_settings],
            ]
            if self.json_path.exists():
                menu_options.insert(0, ["P", "删除已保存的USB数据", self.remove_historical])
            for i in menu_options:
                menu.add_menu_option(i[1], None, i[2], i[0])

            response = menu.start()
        self.on_quit()
        self.utils.custom_quit()

"""骨架模板引擎：根据芯片画像渲染 STM32 工程骨架文件（main/it/system/startup/linker），渲染逻辑与常量数据、字符串模板分离。"""
import json
import logging
import re
from pathlib import Path
from typing import Any

from contracts.exceptions import KnowledgeIOError
from contracts.interfaces import IChipProfile, IMxSkeleton
from infrastructure.chip_family import ChipFamily, derive_linker_name, get_family
from infrastructure.config import DEFAULT_CHIP_NAME, REFERENCE_TEMPLATES_INC  # 外部附属库
from knowledge.loaders.mx_skeleton_data import (
    ALWAYS_ENABLED_HAL_MODULES,
    F407_CLOCK_NOTES,
    F407_DEFAULTS,
    HAL_MODULE_DEPENDENCIES,
    PERIPHERAL_TO_HAL_MODULES,
    PHY_MACRO_DEFS,
    _tool_version,
)
from knowledge.loaders.mx_skeleton_templates import (
    LINKER_LD_TEMPLATE,
    PROJECT_INFO_TEMPLATE,
    get_family_templates,
)

# 功能模板名 → HAL 外设名（hal_conf 模块启用；真编译验证补充）
_FUNC_TO_HAL_PERI: dict[str, str] = {
    "led_blink": "GPIO", "button_read": "GPIO", "gpio_exti": "GPIO", "gpio_multi_out": "GPIO",
    "uart_print": "UART", "uart_interrupt": "UART", "uart_dma": "UART",
    "pwm_output": "TIM", "pwm_servo": "TIM", "tim_periodic": "TIM", "tim_input_capture": "TIM",
    "spi_master": "SPI", "i2c_scan": "I2C", "i2c_sensor": "I2C",
    "adc_read": "ADC", "adc_dma_scan": "ADC", "dac_output": "DAC",
    "dma_mem_copy": "DMA", "rtc_calendar": "RTC", "can_communication": "CAN",
    "sd_card": "SDIO", "iwdg_refresh": "IWDG", "wwdg_refresh": "WWDG",
    "crc_compute": "CRC", "rng_random": "RNG", "system_reset": "IWDG",
}

# ========== 三层架构骨架模板（应用层 / 业务层，范式级通用，不随芯片族变） ==========
# 生成代码从「main.c 平铺」升级为「应用/业务/驱动」三层，
# 依赖方向单向（应用层 → 业务层 → 驱动层）。main.c（驱动层硬件初始化）只调
# App_Loop()（应用层），业务逻辑（原 while(1) 里的 main_loop）搬到 App_Business_Run()（业务层）。

APP_MAIN_H_TEMPLATE = """/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : app_main.h
  * @brief          : 应用层入口（任务调度 + 异常兜底），三层架构的应用层
  ******************************************************************************
  */
/* USER CODE END Header */
#ifndef APP_MAIN_H
#define APP_MAIN_H

#ifdef __cplusplus
extern "C" {
#endif

/* 应用层入口：main.c 只调这两个，业务/驱动分层封装 */
void App_Init(void);
void App_Loop(void);

/* 应用层掉线兜底心跳：业务层每轮踢一脚，超时未踢触发 fallback */
void App_Guard_Feed(void);

#ifdef __cplusplus
}
#endif

#endif /* APP_MAIN_H */
"""

APP_MAIN_C_TEMPLATE = """/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : app_main.c
  * @brief          : 应用层（任务调度 + 异常兜底）
  ******************************************************************************
  * 三层架构应用层：只做调度/编排/兜底，不碰寄存器、不实现具体功能。
  ******************************************************************************
  */
/* USER CODE END Header */

/* Includes ------------------------------------------------------------------*/
#include <stdint.h>   /* uint32_t（心跳计数） */
#include "app_main.h"
#include "app_business.h"
__SYSTEM_INCLUDES__

/* Private define ------------------------------------------------------------*/
/* 掉线兜底超时阈值（主循环轮次）。默认 0xFFFFFFFF = 永不触发（零副作用）；
   要启用掉线兜底时按业务节奏调小，并在业务层每轮 App_Guard_Feed() 踢心跳。 */
#define APP_GUARD_TIMEOUT  0xFFFFFFFFUL

/* Private variables ---------------------------------------------------------*/
/* 掉线兜底心跳计数：业务层每轮踢一脚清零，超时未踢进 fallback */
static volatile uint32_t s_guard_tick = 0;

/* Private function prototypes -----------------------------------------------*/
static void App_Guard_Tick(void);

/**
  * @brief  应用层初始化：业务层初始化接线
  */
void App_Init(void)
{
  App_Business_Init();
}

/**
  * @brief  应用层主循环：业务调度 + 异常兜底（main.c 的 while(1) 只调这里，
  *         调度全景一眼可见）
  */
void App_Loop(void)
{
  /* ⓪ 系统保命（看门狗喂狗等，无条件跑，不被启动门挡住——上电即保命） */
__SYSTEM_LOOP_BODY__
  /* ① 业务调度（业务层：功能实现 + 防御） */
  App_Business_Run();

  /* ② 应用层异常兜底（掉线/超时 fallback 框架） */
  App_Guard_Tick();
}

/**
  * @brief  掉线兜底：业务层每轮踢心跳，超时未踢 → fallback 接线点
  *         （默认不触发；业务层卡死/掉线场景下在此复位 / 重初始化 / 标脏上报）
  */
static void App_Guard_Tick(void)
{
  if (s_guard_tick < APP_GUARD_TIMEOUT)
  {
    s_guard_tick++;
  }
  else
  {
    /* fallback 接线点：掉线/卡死兜底（默认留空，避免误触发） */
    /* 例：NVIC_SystemReset() / 重初始化 / 标脏上报 */
    s_guard_tick = 0;  /* 触发后重置计数，避免永久卡在 fallback 分支 */
  }
}

/**
  * @brief  业务层踢心跳：正常运行时每轮调用，通知应用层「我还活着」
  */
void App_Guard_Feed(void)
{
  s_guard_tick = 0;
}
"""

APP_BUSINESS_H_TEMPLATE = """/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : app_business.h
  * @brief          : 业务层（功能实现 + 防御），三层架构的业务层
  ******************************************************************************
  */
/* USER CODE END Header */
#ifndef APP_BUSINESS_H
#define APP_BUSINESS_H

#ifdef __cplusplus
extern "C" {
#endif

/* 业务层：功能实现 + 防御（不碰寄存器，只调驱动层接口） */
void App_Business_Init(void);
void App_Business_Run(void);

#ifdef __cplusplus
}
#endif

#endif /* APP_BUSINESS_H */
"""

# 启动门代码（有源器件「响/亮」才注入）：上电待机，按 KEY0 启动、再按关闭（有始有终）。
# 纯无源回传工程（RNG/CRC/DMA/定时器/DAC）无源无噪音，不注入。
STARTUP_GATE_CODE = """  /* 按键启动门：toggle 开关——上电待机，按 KEY0 启动、
     再按 KEY0 关闭（有始有终）。关闭沿执行 off 清理（有源器件复位：蜂鸣器停/灯灭）。 */
  static bool s_gate_was_started = false;
  if (!bsp_startup_gate_ready())
  {
    if (s_gate_was_started)
    {
      /* 关闭沿：复位有源器件到安全状态 */
__MAIN_LOOP_OFF__
    }
    s_gate_was_started = false;
    return;
  }
  s_gate_was_started = true;
"""

# 业务层 .c 模板：{peripheral_includes} 注入外设头（让业务代码能引用外设句柄），
# __MAIN_LOOP_BODY__ 由 .replace() 注入原 main_loop 业务代码（不转义大括号）。
APP_BUSINESS_C_TEMPLATE = """/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : app_business.c
  * @brief          : 业务层（功能实现 + 防御），三层架构的业务层
  ******************************************************************************
  * 业务层不直接操作寄存器，只调驱动层（{periph}.c / bsp_*.c）接口；
  * 防御件（中值滤波 / 消抖 / 值域钳位）在此组合。
  ******************************************************************************
  */
/* USER CODE END Header */

/* Includes ------------------------------------------------------------------*/
#include "app_business.h"
#include <stdio.h>
#include <string.h>
{peripheral_includes}
{defense_includes}

/* 非阻塞周期执行辅助（「」：业务层周期任务用 APP_EVERY_MS 判周期，
   不用阻塞 HAL_Delay——阻塞会拖慢同层其他任务，组合模板时按键检测被 uart 打印的
   delay 拖成 1 秒一次）。_last 是 static uint32_t 上次执行时间戳。 */
#define APP_EVERY_MS(_last, _ms) \
    ((HAL_GetTick() - (_last) >= (uint32_t)(_ms)) ? ((_last) = HAL_GetTick(), 1) : 0)

/* 业务层初始化：组合防御件（掉线检测 / 滤波 / Flash 参数读回）在此接线 */
void App_Business_Init(void)
{
  /* USER CODE BEGIN Business_Init */
  /* USER CODE END Business_Init */
}

/* 业务层调度：功能模板生成的业务逻辑（原 while(1) 里的 main_loop） */
void App_Business_Run(void)
{
  /* USER CODE BEGIN Business */
__STARTUP_GATE__
__MAIN_LOOP_BODY__
  /* USER CODE END Business */
}
"""

logger = logging.getLogger(__name__)


def _clock_regs_from_tree(tree: dict[str, Any]) -> dict[str, Any]:
    """把 profile.json 的 clock_tree 映射成模板占位符值（_regs 字段）。

    F4/G4 用 pll_m/n/p/q，F1 用 pll_mult（倍频）。pll_p 是 int（2）→ 宏名
    RCC_PLLP_DIV2；pll_mult 是 int（9）→ 宏名 RCC_PLL_MUL9。clock_tree 缺失的字段
    不写入，由 F407_DEFAULTS 兜底。
    """
    regs: dict[str, Any] = {}
    if "pll_m" in tree:
        regs["pll_m"] = tree["pll_m"]
    if "pll_n" in tree:
        regs["pll_n"] = tree["pll_n"]
    if "pll_p" in tree:
        regs["pll_p"] = f"RCC_PLLP_DIV{tree['pll_p']}"
    if "pll_q" in tree:
        regs["pll_q"] = tree["pll_q"]
    if "pll_mult" in tree:
        regs["pll_mult"] = f"RCC_PLL_MUL{tree['pll_mult']}"
    if "flash_latency" in tree:
        regs["flash_latency"] = tree["flash_latency"]
    if tree.get("voltage_scale"):
        regs["voltage_scale"] = tree["voltage_scale"]
    if "hse_hz" in tree:
        regs["hse_value"] = tree["hse_hz"]
    if "sysclk_hz" in tree:
        regs["sysclk_hz"] = tree["sysclk_hz"]
    return regs


class MxSkeleton(IMxSkeleton):
    """骨架模板引擎 — 按芯片族选择模板集合，渲染对应工程文件。"""

    def __init__(self, profile: IChipProfile, fcnt_module: Any = None) -> None:
        self.profile = profile
        self._family = get_family(getattr(self.profile, "chip_name", DEFAULT_CHIP_NAME))
        self._templates = get_family_templates(self._family.name)

        # 具体芯片时钟树（profile.json 的 clock_tree）→ 模板字段（空壳·细化核心：
        # 非 F407 芯片的 SystemClock_Config 不再用 F407 硬编码，改读具体芯片时钟树）
        clock_regs = _clock_regs_from_tree(getattr(self.profile, "clock_tree", {}) or {})

        # 优先从 FCNT 模块取值，否则用 F407 默认值
        self._clock_notes = F407_CLOCK_NOTES
        notes: dict[str, Any] = {}
        if fcnt_module:
            try:
                notes = fcnt_module.get_register_notes() or {}
                self._clock_notes = fcnt_module.notes.get("clock_notes", F407_CLOCK_NOTES)
            except (KnowledgeIOError, OSError, json.JSONDecodeError):
                notes = {}

        # 优先级：clock_tree（具体芯片权威）> FCNT notes > F407_DEFAULTS（系列兜底）
        self._regs = {**F407_DEFAULTS, **notes, **clock_regs}

    def _get(self, key: str, default: str = "") -> str:
        return str(self._regs.get(key, default))

    def generate_main_h(self, peripheral_headers: list[str] | None = None) -> str:
        peripherals = peripheral_headers or []
        proto_lines = ""
        for p in peripherals:
            proto_lines += f"void MX_{p}_Init(void);\n"

        return self._templates.main_h_template.format(
            chip_name=self.profile.chip_name,
            core=self.profile.core,
            clock=self.profile.max_clock_mhz,
            flash=self.profile.flash_kb,
            ram=self.profile.ram_kb,
            ccm=self.profile.ccm_kb,
            peripheral_prototypes=proto_lines,
        )

    def generate_main_c(
        self,
        peripheral_includes: list[str] | None = None,
        peripheral_handles: list[str] | None = None,
        peripheral_inits: list[str] | None = None,
        peripheral_init_protos: list[str] | None = None,
        main_loop_body: list[str] | None = None,
    ) -> str:
        inc_lines = "\n".join(peripheral_includes or [])
        handle_lines = "\n".join(peripheral_handles or [])
        init_lines = "\n".join(peripheral_inits or [])
        proto_lines = "\n".join(peripheral_init_protos or [])
        loop_lines = "\n".join(main_loop_body or ["    /* USER CODE */"])

        led_port, led_pin_macro = self._error_led_info()

        return self._templates.main_c_header.format(
            chip_name=self.profile.chip_name,
            core=self.profile.core,
            clock=self.profile.max_clock_mhz,
            flash=self.profile.flash_kb,
            ram=self.profile.ram_kb,
            ccm=self.profile.ccm_kb,
            clock_notes=self._clock_notes,
            voltage_scale=self._get("voltage_scale", "PWR_REGULATOR_VOLTAGE_SCALE1"),
            pll_m=self._get("pll_m", "8"),
            pll_n=self._get("pll_n", "336"),
            pll_p=self._get("pll_p", "RCC_PLLP_DIV2"),
            pll_q=self._get("pll_q", "7"),
            pll_mult=self._get("pll_mult", "RCC_PLL_MUL9"),
            flash_latency=self._get("flash_latency", "FLASH_LATENCY_5"),
            peripheral_includes=inc_lines,
            peripheral_handles=handle_lines,
            peripheral_init_protos=proto_lines,
            peripheral_inits=init_lines,
            main_loop_body=loop_lines,
            error_led_port=led_port,
            error_led_pin_macro=led_pin_macro,
            tool_version=_tool_version(),
        )

    def generate_app_business_c(
        self,
        peripheral_includes: list[str] | None = None,
        main_loop_body: str = "",
        defense_units: tuple[str, ...] = (),
        off_body: str = "",
    ) -> str:
        """生成业务层 app_business.c：外设头 include + 防御头（按环节）+ App_Business_Run。

        三层架构 ：原 main.c while(1) 里的业务代码（main_loop）
        搬到业务层 App_Business_Run()，业务层不碰寄存器、只调驱动层接口。
        defense_units（「」）：功能模板声明的防御件，按声明 include 对应头。
        off_body（「」）：启动门 toggle 到关闭沿时执行的有源器件复位动作。
        """
        from knowledge.template_forge.defense_injector import defense_include_lines

        inc_lines = "\n".join(peripheral_includes or [])
        defense_inc = "\n".join(defense_include_lines(defense_units))
        # 启动门只在「有有源器件（defense_units 含 startup_gate）」时注入；纯无源回传
        # 工程不注入，App_Business_Run 直接跑业务。
        has_gate = "startup_gate" in defense_units
        gate_code = STARTUP_GATE_CODE if has_gate else ""
        return APP_BUSINESS_C_TEMPLATE.replace(
            "{peripheral_includes}", inc_lines
        ).replace("{defense_includes}", defense_inc).replace(
            "__STARTUP_GATE__", gate_code
        ).replace(
            "__MAIN_LOOP_BODY__", main_loop_body
        ).replace("__MAIN_LOOP_OFF__", off_body)

    def generate_periph_c(self, fname: str, slot: dict[str, Any]) -> str:
        """生成外设 .c 文件（MX_xxx_Init/DeInit 函数体 + 句柄定义 + 回调）。

        slot 来自 bundles_to_project_slices 的 peripherals[fname]。
        """
        globals_code = "\n".join(slot.get("globals", []) or [])
        periph = str(slot.get("periph", fname))

        defs: list[str] = []
        for fn, bodies in (slot.get("init_bodies") or {}).items():
            # 合并同名 MX_xxx_Init 函数体时去重局部变量声明（「」：
            # 按键控制点灯 = led_blink + button_read 都声明 GPIO_InitStruct，直接拼会重复定义）
            from knowledge.template_forge.project_slicer import _strip_dup_decls

            seen: set[str] = set()
            merged = [_strip_dup_decls(b, seen) for b in bodies]
            # F1 的 ADC_InitTypeDef 缺 F4 独有字段（对称面：GPIO Alternate 过滤，这里补 ADC 字段过滤）
            if self._is_f1_adc(slot):
                merged = [self._strip_f1_adc_fields(b) for b in merged]
            defs.append(f"void {fn}(void)\n{{\n" + "\n\n".join(merged) + "\n}")
        for fn, bodies in (slot.get("deinit_bodies") or {}).items():
            defs.append(f"void {fn}(void)\n{{\n" + "\n\n".join(bodies) + "\n}")

        extra = "\n\n".join(slot.get("extra_code", []) or [])
        if extra:
            defs.append("/* USER CODE BEGIN 1 */\n" + extra + "\n/* USER CODE END 1 */")

        return (
            "/* USER CODE BEGIN Header */\n"
            "/**\n"
            f"  * @file           : {fname}.c\n"
            f"  * @brief          : {periph} peripheral configuration (Agent-S Forge)\n"
            "  ******************************************************************************\n"
            "  */\n"
            "/* USER CODE END Header */\n\n"
            "/* Includes ------------------------------------------------------------------*/\n"
            f'#include "{fname}.h"\n\n'
            "/* USER CODE BEGIN 0 */\n"
            f"{globals_code}\n"
            "/* USER CODE END 0 */\n\n"
            + "\n\n".join(defs)
            + "\n"
        )

    def _is_f1_adc(self, slot: dict[str, Any]) -> bool:
        """判断当前 slot 是否 F1 系列的 ADC（需过滤 F4 独有字段）。"""
        periph = str(slot.get("periph", "") or "").upper()
        return (not self._family.gpio_alternate) and periph.startswith("ADC")

    @staticmethod
    def _strip_f1_adc_fields(body: str) -> str:
        """过滤 F1 ADC_InitTypeDef 不存在的 F4 独有字段行（ClockPrescaler/Resolution/
        EOCSelection/LowPowerAutoWait）。对称面：GPIO 的 Alternate 过滤（generate_hal_msp_c）。"""
        return re.sub(
            r"[ \t]*\w+\.Init\.(?:ClockPrescaler|Resolution|EOCSelection|LowPowerAutoWait)"
            r"\s*=[^\n]*\n?",
            "",
            body,
        )

    def generate_periph_h(self, fname: str, slot: dict[str, Any]) -> str:
        """生成外设 .h 文件（保护宏 + 句柄 extern + 函数原型）。"""
        guard = f"__{fname.upper()}_H"
        # extern 声明须去掉初始化器（uint32_t data[16] = {...}; → uint32_t data[16];），
        # 否则 .h 生成 extern+初始化 = 非法重复定义（全模板编译验证）。
        # 注意 slot["globals"] 存的是「多行字符串」合并的一个元素，须按行拆分再逐行生成。
        def _strip_init(g: str) -> str:
            g = g.strip()
            if "=" in g:
                g = g.split("=", 1)[0].rstrip() + ";"
            return g

        _all_globals = "\n".join(slot.get("globals", []) or []).split("\n")
        extern_handles = "\n".join(
            f"extern {_strip_init(g)}" for g in _all_globals if g.strip()
        )
        protos: list[str] = []
        for fn in (slot.get("init_bodies") or {}):
            protos.append(f"void {fn}(void);")
        for fn in (slot.get("deinit_bodies") or {}):
            protos.append(f"void {fn}(void);")
        # MspInit 声明（虚拟 MspInit 关键）：GPIO 无 ST 库声明
        # （stm32f4xx_hal_gpio.h 无 HAL_GPIO_MspInit），gpio.c 手动调用需在此显式声明，
        # 否则 C99 报「implicit function declaration」。其余外设（UART/TIM…）ST 库已有，
        # 显式声明无害（签名与 hal_msp.c 定义一致）。
        msp_fn = str(slot.get("msp_fn", "") or "")
        msp_param = str(slot.get("msp_param", "") or "")
        if msp_fn:
            protos.append(f"void {msp_fn}({msp_param});")
        proto_block = "\n".join(protos)
        return (
            f"#ifndef {guard}\n"
            f"#define {guard}\n\n"
            "#ifdef __cplusplus\n"
            'extern "C" {\n'
            "#endif\n\n"
            '/* Includes ------------------------------------------------------------------*/\n'
            '#include "main.h"\n\n'
            "/* Exported functions prototypes ---------------------------------------------*/\n"
            f"{extern_handles}\n"
            f"{proto_block}\n\n"
            "#ifdef __cplusplus\n"
            "}\n"
            "#endif\n\n"
            f"#endif /* {guard} */\n"
        )

    def generate_hal_msp_c(self, periphs: dict[str, dict[str, Any]]) -> str:
        """生成 hal_msp.c（HAL_MspInit + 各外设 HAL_xxx_MspInit：时钟使能 + 引脚复用）。

        修 F1 跨系列：F1 的 stm32f1xx_hal.h 不集中 include 外设头（F4 会按
        模块宏集中 include），故 hal_msp.c 需显式 include 用到的外设头
        （stm32f1xx_hal_gpio.h 等），否则 GPIO_InitTypeDef/UART_HandleTypeDef 未定义。
        F4/G4 重复 include 无害（有 include guard）。
        """
        from knowledge.template_forge.hal_parser import _PERI_TO_HEADER_SUFFIX

        family_prefix = self._family.name.lower()
        headers: list[str] = []
        for _fname, slot in periphs.items():
            p = str(slot.get("periph", "") or _fname).upper()
            base = re.sub(r"\d+$", "", p)
            base = {"USART": "UART", "FSMC": "SRAM"}.get(base, base)
            suffix = _PERI_TO_HEADER_SUFFIX.get(base.lower())
            if suffix:
                headers.append(f'#include "{family_prefix}_{suffix}"')
        include_block = "\n".join(sorted(set(headers)))

        parts: list[str] = [
            "/* Includes ------------------------------------------------------------------*/\n"
            '#include "main.h"\n'
            + (include_block + "\n" if include_block else "")
            + "\n/**\n"
            "  * @brief  Initializes the Global MSP.\n"
            "  * @retval None\n"
            "  */\n"
            "void HAL_MspInit(void)\n"
            "{\n"
            "  __HAL_RCC_SYSCFG_CLK_ENABLE();\n"
            "  __HAL_RCC_PWR_CLK_ENABLE();\n"
            "}\n"
        ]
        is_f1 = not self._family.gpio_alternate  # 材料驱动：F1 无 Alternate 字段（family.json 声明）
        for _fname, slot in periphs.items():
            msp_code = list(dict.fromkeys(slot.get("msp_code", []) or []))  # 去重保序
            if is_f1:
                # F1 无 Alternate 字段（AF 复用隐式，Mode=AF_PP 即可），过滤 .Alternate 行
                msp_code = [ln for ln in msp_code if ".Alternate" not in ln]
            if not msp_code:
                continue
            msp_fn = slot.get("msp_fn", "")
            msp_param = slot.get("msp_param", "")
            param_name = msp_param.rsplit("*", 1)[-1].strip() if msp_param else "handle"
            body = "\n".join(msp_code)
            parts.append(
                f"void {msp_fn}({msp_param})\n"
                "{\n"
                f"  (void){param_name};\n"
                f"{body}\n"
                "}\n"
            )
        return "\n\n".join(parts)

    def _error_led_info(self) -> tuple[str, str]:
        """解析错误 LED 引脚，返回 (GPIOx, GPIO_PIN_n)。"""
        pin = getattr(self.profile, "error_led_pin", "PA5")
        if not pin:
            pin = "PA5"
        match = re.match(r"P([A-K])(\d{1,2})", pin, re.IGNORECASE)
        if not match:
            return "GPIOA", "GPIO_PIN_5"
        port_letter = match.group(1).upper()
        pin_num = match.group(2)
        return f"GPIO{port_letter}", f"GPIO_PIN_{pin_num}"

    def generate_it_c(
        self,
        extern_handles: list[str] | None = None,
        irq_handlers: list[str] | None = None,
    ) -> str:
        return self._templates.it_c_template.format(
            extern_handles="\n".join(extern_handles or []),
            peripheral_irq_handlers="\n".join(irq_handlers or []),
            tool_version=_tool_version(),
        )

    def generate_it_h(self, irq_protos: list[str] | None = None) -> str:
        return self._templates.it_h_template.format(
            peripheral_irq_protos="\n".join(irq_protos or []),
        )

    def generate_system(self) -> str:
        """生成 system 源文件。

        G4 系列优先使用官方 system_stm32g4xx.c（reference-stm32g4/
        device/Include/，寄存器布局与 F4 不同——F4 模板用 RCC->CIR 等 G4 没有的
        寄存器）；其他族用模板参数化生成。
        """
        family_name = getattr(self._family, "name", "")
        if family_name.startswith("STM32G4"):
            try:
                from infrastructure.config import reference_dir_for_family

                ref = reference_dir_for_family(family_name)
                candidate = ref / "device" / "Include" / "system_stm32g4xx.c"
                if candidate.exists():
                    return candidate.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 —— 官方文件不可用回退模板
                pass
        return self._templates.system_template.format(
            clock_hz=self.profile.max_clock_mhz * 1_000_000,
            hse_value=self._get("hse_value", "8000000"),
            hsi_value=self._get("hsi_value", "16000000"),
            tool_version=_tool_version(),
        )

    def generate_project_info(
        self,
        project_name: str,
        peripherals: list[str],
        peripheral_headers: list[str] | None = None,
        timestamp: str = "",
    ) -> str:
        peri_list = "\n".join(f"- {p}" for p in peripherals)
        headers = "\n".join(f"│   │   ├── {h}" for h in (peripheral_headers or []))
        return PROJECT_INFO_TEMPLATE.format(
            project_name=project_name,
            chip_name=self.profile.chip_name,
            core=self.profile.core,
            clock=self.profile.max_clock_mhz,
            flash=self.profile.flash_kb,
            ram=self.profile.ram_kb,
            ccm=self.profile.ccm_kb,
            timestamp=timestamp,
            peripheral_list=peri_list,
            peripheral_headers=headers,
            tool_version=_tool_version(),
            it_h_file=f"{self._family.name.lower()}_it.h",
            it_c_file=self._family.it_file,
            system_file=self._family.system_file,
            family=self._family.name,
        )

    def generate_makefile(self, chip_name: str | None = None) -> str:
        """生成标准 Makefile（GNU arm-none-eabi-gcc，编译链接 elf + hex/bin）。

        「」：标准 CubeMX 工程含 Makefile，此前 MxSkeleton 未生成，
        只靠 compile_check 用 Python 调 gcc。现补全——源文件用 wildcard 自动包含
        Core/Src/*.c（外设独立文件 + hal_msp.c + it.c + system.c + main.c），
        改了源文件无需改 Makefile。
        """
        name = (chip_name or str(getattr(self.profile, "chip_name", DEFAULT_CHIP_NAME))).upper()
        family = self._family
        defines = " ".join("-D" + d for d in family.defines)
        linker_name = self._linker_file_name(chip_name)
        # 跨系列活接口：HAL 驱动路径/源前缀从 family 材料推导，不再写死 F4。
        # system_stm32f4xx.c → stm32f4xx；变体（STM32F4xx_F446）的 device 目录取基础系列 STM32F4xx。
        hal_prefix = family.system_file.replace("system_", "").replace(".c", "")
        device_family = family.name.split("_")[0]
        # ST 官方固件（HAL/CMSIS 头 + HAL 源），与 compile_check 同源。
        # 脱敏：env STM32_CUBE_FW 可覆盖，默认 CubeMX 标准安装路径——
        # 开源后别人 clone 不装 ST 固件也能通过 env 指向自己的。
        st_fw = "$(HOME)/STM32Cube/Repository/STM32Cube_FW_F4_V1.28.3"
        # 工具链自动定位（「」）：make 直接跑会因 arm-none-eabi-gcc
        # 不在 PATH 而失败，故默认写死 Agent-S 内化工具链的 bin 目录（_find_arm_gcc 四级查找）。
        # 脱敏：env ARM_GCC_PATH 可覆盖，开源剥离后工具链不随仓库走，走 PATH 的 arm-none-eabi-gcc。
        gcc_bin = ""
        try:
            from infrastructure.makefile_generator import _find_arm_gcc

            _p = _find_arm_gcc()
            if _p:
                gcc_bin = str(Path(_p).parent).replace("\\", "/")
        except Exception:  # noqa: BLE001 —— 工具链定位失败则回退 PATH
            gcc_bin = ""
        if gcc_bin:
            prefix_lines = [
                "# 工具链：env ARM_GCC_PATH 优先，其次自动定位的内化工具链，可 make GCC_PATH=... 覆盖",
                f"ARM_GCC_PATH ?= {gcc_bin}",
                "GCC_PATH ?= $(ARM_GCC_PATH)",
                "PREFIX = $(GCC_PATH)/arm-none-eabi-",
            ]
        else:
            prefix_lines = [
                "# 工具链：未定位到内化工具链，走 PATH 的 arm-none-eabi-gcc",
                "PREFIX ?= arm-none-eabi-",
            ]
        return "\n".join([
            f"# Agent-S Generated Makefile — {name} ({family.name})",
            "TARGET = firmware",
            *prefix_lines,
            "CC = $(PREFIX)gcc",
            "CP = $(PREFIX)objcopy",
            "SZ = $(PREFIX)size",
            "",
            "# ST 官方固件（HAL/CMSIS）：env STM32_CUBE_FW 可覆盖，默认 CubeMX 标准安装路径",
            f"STM32_CUBE_FW ?= {st_fw}",
            "ST_FW ?= $(STM32_CUBE_FW)",
            f"HAL_INC = $(ST_FW)/Drivers/{family.hal_family_dir}/Inc",
            f"HAL_SRC = $(ST_FW)/Drivers/{family.hal_family_dir}/Src",
            "CMSIS_INC = $(ST_FW)/Drivers/CMSIS/Include",
            f"DEVICE_INC = $(ST_FW)/Drivers/CMSIS/Device/ST/{device_family}/Include",
            "",
            "# 源文件：Core/Src 下所有 .c（外设独立文件 + hal_msp + it + system + main）",
            "C_SOURCES = $(wildcard Core/Src/*.c)",
            f"ASM_SOURCES = Core/Startup/{family.startup_pattern}",
            f"HAL_SOURCES = $(filter-out %_template.c, $(wildcard $(HAL_SRC)/{hal_prefix}_hal*.c))",
            # LL 层也 wildcard 匹配（SRAM 编译）：FSMC/SRAM 的 LL 层
            # （stm32f4xx_ll_fsmc.c 的 FSMC_NORSRAM_Extended_Timing_Init）此前手动只加
            # ll_sdmmc.c，漏了 ll_fsmc.c → undefined reference。LL 文件都带模块 #ifdef
            # 保护，未使能的编译成空，wildcard 全收无害，逐个手动加才容易漏。
            f"HAL_SOURCES += $(filter-out %_template.c, $(wildcard $(HAL_SRC)/{hal_prefix}_ll*.c))",
            "",
            "C_INCLUDES = -ICore/Inc -I$(HAL_INC) -I$(CMSIS_INC) -I$(DEVICE_INC)",
            f"C_DEFS = {defines}",
            f"CPU = -mcpu={family.cpu_flag}",
            f"FPU = {family.fpu_flag}",
            f"LDSCRIPT = {linker_name}",
            "CFLAGS = $(CPU) $(FPU) -mthumb -O1 -g -Wall $(C_DEFS) $(C_INCLUDES)",
            "LDFLAGS = $(CPU) $(FPU) -mthumb --specs=nano.specs --specs=nosys.specs -Wl,--gc-sections -T$(LDSCRIPT)",
            "",
            "# 编译规则（根修「.o 污染」）：HAL 库 .o 隔离到工程 build/ 目录，",
            "# 不再落到 STM32Cube_FW/Src 共享目录——否则跨工程 .o 残留，hal_conf.h 模块使能",
            "# 不同导致「空壳 .o」被复用、HAL_xxx_Init undefined reference（系统复位编译失败根因）。",
            "HAL_OBJS = $(addprefix build/, $(notdir $(HAL_SOURCES:.c=.o)))",
            "C_OBJS   = $(addprefix build/, $(notdir $(C_SOURCES:.c=.o)))",
            "ASM_OBJS = $(addprefix build/, $(notdir $(ASM_SOURCES:.s=.o)))",
            "OBJS = $(C_OBJS) $(ASM_OBJS) $(HAL_OBJS)",
            "",
            "all: $(TARGET).elf $(TARGET).hex $(TARGET).bin",
            "",
            "# 链接脚本是 elf 的依赖：改 .ld（RAM/CCM 大小）必须触发重链",
            "$(TARGET).elf: $(OBJS) $(LDSCRIPT)",
            "\t$(CC) $(LDFLAGS) -o $@ $(OBJS)",
            "\t$(SZ) $@",
            "",
            "build/%.o: $(HAL_SRC)/%.c",
            "\t@mkdir -p build",
            "\t$(CC) $(CFLAGS) -c -o $@ $<",
            "",
            "build/%.o: Core/Src/%.c",
            "\t@mkdir -p build",
            "\t$(CC) $(CFLAGS) -c -o $@ $<",
            "",
            "build/%.o: Core/Startup/%.s",
            "\t@mkdir -p build",
            "\t$(CC) $(CPU) $(FPU) -mthumb -c -o $@ $<",
            "",
            "$(TARGET).hex: $(TARGET).elf",
            "\t$(CP) -O ihex $< $@",
            "",
            "$(TARGET).bin: $(TARGET).elf",
            "\t$(CP) -O binary -S $< $@",
            "",
            "clean:",
            "\trm -f $(OBJS) $(TARGET).elf $(TARGET).hex $(TARGET).bin",
            "",
            ".PHONY: all clean",
        ])

    @property
    def family(self) -> ChipFamily:
        """返回当前芯片族元数据。"""
        return self._family

    def generate_startup_s(self) -> str:
        """生成启动汇编文件（按芯片族选择模板）。

        G4 系列优先使用官方 GCC 启动文件（reference-stm32g4/startup/，
        中断向量表与 F4 不同）；找不到/其他族回退模板。
        """
        family_name = getattr(self._family, "name", "")
        if family_name.startswith("STM32G4"):
            try:
                from infrastructure.config import reference_dir_for_family

                ref = reference_dir_for_family(family_name)
                pattern = getattr(self._family, "startup_pattern", "startup_stm32g431xx.s")
                candidate = ref / "startup" / pattern
                if candidate.exists():
                    return candidate.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 —— 官方文件不可用回退模板
                pass
        return self._templates.startup_template

    def generate_linker_ld(self, chip_name: str | None = None) -> str:
        """生成链接脚本，按芯片画像的 Flash/RAM/CCMRAM（材料驱动，不再写死 MEMORY_MAPS）。

        修 P0：此前 `_memory_map(name)` 走写死 MEMORY_MAPS（缺 G4），
        G431 被兜底成 F407 的 1024/128/64 → .ld 内存分区全错。改从 self.profile
        （profile.json 权威材料）读，与 generate_main_c/generate_project_info 一致。
        """
        name = (chip_name or str(getattr(self.profile, "chip_name", DEFAULT_CHIP_NAME))).upper()
        flash_kb = int(getattr(self.profile, "flash_kb", 0) or 0)
        ram_kb = int(getattr(self.profile, "ram_kb", 0) or 0)
        ccram_kb = int(getattr(self.profile, "ccm_kb", 0) or 0)
        linker_file_name = derive_linker_name(name)

        if ccram_kb > 0:
            ccm_section = f"CCMRAM (xrw)   : ORIGIN = 0x10000000, LENGTH = {ccram_kb}K\n"
            ccmram_sections = """  _siccmram = LOADADDR(.ccmram);

  /* CCM-RAM section */
  .ccmram :
  {
    . = ALIGN(4);
    _sccmram = .;
    *(.ccmram)
    *(.ccmram*)

    . = ALIGN(4);
    _eccmram = .;
  } >CCMRAM AT> FLASH
"""
        else:
            ccm_section = ""
            ccmram_sections = ""

        return LINKER_LD_TEMPLATE.format(
            chip_name=name,
            linker_file_name=linker_file_name,
            flash=flash_kb,
            ram=ram_kb,
            ccm_section=ccm_section,
            ccmram_sections=ccmram_sections,
        )

    def _resolve_enabled_hal_modules(self, enabled_modules: list[str] | None) -> set[str]:
        """根据请求的外设列表解析出需要启用的 HAL 模块宏集合（含依赖）。"""
        requested = {f"HAL_{m.upper()}_MODULE_ENABLED" for m in (enabled_modules or [])}
        for module in list(requested):
            base = module.replace("HAL_", "").replace("_MODULE_ENABLED", "")
            requested.update(PERIPHERAL_TO_HAL_MODULES.get(base, set()))

        extra_enabled: set[str] = set()
        for macro in requested:
            extra_enabled.update(HAL_MODULE_DEPENDENCIES.get(macro, set()))
        return ALWAYS_ENABLED_HAL_MODULES | requested | extra_enabled

    def _inject_phy_macros(self, content: str) -> str:
        """ETH 启用时在 hal_conf.h 末尾注入 PHY 基础宏定义。"""
        device_family = self._family.name.split("_")[0]  # 变体取基础系列（STM32F4xx_F446 → STM32F4xx）
        return re.sub(
            r"#endif /\* __STM32\w+_HAL_CONF_H \*/",
            f"{PHY_MACRO_DEFS}\n#endif /* __{device_family}_HAL_CONF_H */",
            content,
        )

    def generate_hal_conf_h(self, enabled_modules: list[str] | None = None) -> str:
        """生成 HAL 配置头（按 family 选择 reference 模板，并按实际外设裁剪模块宏）。

        芯片数据扩充：模板路径按 family 的 reference 目录解析
        （F1→reference-stm32f1 / F4→reference-stm32f4 / G4→reference-stm32g4），
        不再只查 F4 库（此前 G4 回退拿到 F4 内容 → include stm32f4xx_hal_*.h 编译失败）。
        """
        family_name = self._family.name.lower()
        try:
            from infrastructure.config import reference_dir_for_family

            ref_root = reference_dir_for_family(self._family.name)
        except Exception:  # noqa: BLE001
            ref_root = Path(REFERENCE_TEMPLATES_INC).parent.parent
        template_path = ref_root / "templates" / "Inc" / f"{family_name}_hal_conf.h"
        if not template_path.exists():
            template_path = (
                Path(REFERENCE_TEMPLATES_INC)  # 外部附属库（F4 兜底）
                / f"{family_name}_hal_conf.h"
            )
        if not template_path.exists():
            template_path = (
                Path(REFERENCE_TEMPLATES_INC)  # 外部附属库
                / "stm32f4xx_hal_conf.h"
            )
        if not template_path.exists():
            return "/* HAL configuration: reference template not found */"

        content = template_path.read_text(encoding="utf-8", errors="replace")
        enabled = self._resolve_enabled_hal_modules(enabled_modules)

        def _disable_if_unused(match: re.Match[str]) -> str:
            macro = match.group(1)
            return f"#define {macro}" if macro in enabled else f"/* #define {macro} */"

        content = re.sub(
            r"^(?:/\*\s*)?#define\s+(HAL_[A-Z0-9_]+_MODULE_ENABLED)\s*(?:\*/)?$",
            _disable_if_unused,
            content,
            flags=re.MULTILINE,
        )

        if "HAL_ETH_MODULE_ENABLED" in enabled:
            content = self._inject_phy_macros(content)

        return content

    def _linker_file_name(self, chip_name: str | None = None) -> str:
        """根据芯片型号推导链接脚本文件名。"""
        return derive_linker_name(chip_name or str(getattr(self.profile, "chip_name", DEFAULT_CHIP_NAME)))

    def build_file_tree(self, peripherals: list[str], project_name: str) -> dict[str, str]:
        from datetime import datetime

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        base = project_name
        family = self._family
        family_prefix = family.name.lower()
        it_h_file = f"{family_prefix}_it.h"
        it_c_file = family.it_file
        system_c_file = family.system_file

        headers = []
        for p in peripherals:
            headers.append(f"{p.lower()}.h")

        # 功能模板名 → HAL 外设名（hal_conf 模块启用用；真编译：
        # 直接传模板名导致 HAL_UART_* 等宏未启用）
        hal_peris = [_FUNC_TO_HAL_PERI.get(p, p.upper()) for p in peripherals]

        files = {
            f"{base}/Core/Inc/main.h": self.generate_main_h([p.upper() for p in peripherals]),
            # hal_conf 按功能启用 HAL 模块（真编译：空启只有 GPIO，
            # TIM/ADC 等模板编译报宏未声明）
            f"{base}/Core/Inc/{family_prefix}_hal_conf.h": self.generate_hal_conf_h(hal_peris),
            f"{base}/Core/Inc/{it_h_file}": self.generate_it_h(),
            f"{base}/Core/Src/main.c": self.generate_main_c(),
            f"{base}/Core/Src/{it_c_file}": self.generate_it_c(),
            f"{base}/Core/Src/{system_c_file}": self.generate_system(),
            f"{base}/Core/Startup/{family.startup_pattern}": self.generate_startup_s(),
            f"{base}/{self._linker_file_name()}": self.generate_linker_ld(),
            f"{base}/project_info.md": self.generate_project_info(
                project_name, peripherals, headers, ts
            ),
        }
        return files

    def _build_manifest(self, slices: dict[str, Any]) -> str:
        """注册清单元数据（串口车间标准接口，schema 见 serial_workshop/manifest_schema.json）。

        「 + 线路打通」：生产车间落盘结构化注册清单，
        串口车间读它把 trace 对齐到外设/中断/引脚。此方法不 import 串口车间，
        只负责产出 JSON——两个车间唯一的交汇点。
        """
        import json as _json

        periphs: dict[str, dict[str, Any]] = slices.get("peripherals", {}) or {}
        main_inits: list[str] = list(slices.get("main_inits", []) or [])

        manifest_peripherals: list[dict[str, Any]] = []
        for fname, slot in periphs.items():
            init_func = next(iter(slot.get("init_bodies", {}) or {}), "")
            deinit_func = next(iter(slot.get("deinit_bodies", {}) or {}), "")
            msp_code = "\n".join(str(c) for c in slot.get("msp_code", []) or [])
            init_code = "\n".join(
                "\n".join(str(v) for v in bodies) for bodies in slot.get("init_bodies", {}).values()
            )
            extra_code = "\n".join(str(c) for c in slot.get("extra_code", []) or [])

            # 引脚：从 MSP + init 体提取 GPIO 端口 + 引脚号（GPIO_InitStruct.Pin = GPIO_PIN_9 → PA9）
            pins: list[str] = []
            _ports = re.findall(r"HAL_GPIO_Init\(\s*GPIO([A-K])\b", msp_code + "\n" + init_code)
            _pin_nums = re.findall(r"GPIO_PIN_(\d+)", msp_code + "\n" + init_code)
            if _ports and _pin_nums:
                pins = sorted({f"P{port}{n}" for port in _ports for n in _pin_nums})

            # 中断：从 extra_code 提取 IRQHandler 定义
            interrupts = sorted(set(re.findall(r"\b(\w+_IRQHandler)\b", extra_code)))

            manifest_peripherals.append({
                "name": str(slot.get("periph", "") or fname),
                "file": fname,
                "init_func": init_func,
                "deinit_func": deinit_func,
                "msp_func": str(slot.get("msp_fn", "") or ""),
                "msp_param": str(slot.get("msp_param", "") or ""),
                "globals": [str(g) for g in slot.get("globals", []) or []],
                "pins": pins,
                "interrupts": interrupts,
            })

        # 初始化顺序：从 main_inits 提取 MX_xxx_Init 函数名
        init_order = re.findall(r"\b(MX_\w+_Init)\b", "\n".join(main_inits))

        manifest: dict[str, Any] = {
            "schema_version": "1.0",
            "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generator": "stm32-forge",
            "chip": {
                "name": str(getattr(self.profile, "chip_name", "")),
                "core": str(getattr(self.profile, "core", "")),
                "sysclk_mhz": float(getattr(self.profile, "max_clock_mhz", 0)),
                "flash_kb": float(getattr(self.profile, "flash_kb", 0)),
                "ram_kb": float(getattr(self.profile, "ram_kb", 0)),
                "ccm_kb": float(getattr(self.profile, "ccm_kb", 0)),
            },
            "peripherals": manifest_peripherals,
            "init_order": init_order,
        }
        return _json.dumps(manifest, ensure_ascii=False, indent=2)

    def build_standard_project(
        self, slices: dict[str, Any], project_name: str, enable_trace: bool = False
    ) -> dict[str, str]:
        """标准工程文件树（严格 CubeMX 结构 + 外设独立文件）。

        slices 来自 bundles_to_project_slices：按外设分组的 init/deinit 函数体、
        hal_msp.c 时钟/引脚复用代码、main.c 的 init 调用。产物：
          Core/Inc/main.h + {periph}.h + hal_conf.h + it.h
          Core/Src/main.c + {periph}.c + hal_msp.c + it.c + system
          Core/Startup/startup.s + linker.ld + project_info.md

        enable_trace（「闭环」）：True 时注入 as_trace 探针
        （AS_PROBE 板内对账，预期值=芯片手册真值），False 保持纯净（默认）。
        """
        from datetime import datetime

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        base = project_name
        family = self._family
        family_prefix = family.name.lower()
        it_h_file = f"{family_prefix}_it.h"
        it_c_file = family.it_file
        system_c_file = family.system_file

        periphs: dict[str, dict[str, Any]] = slices.get("peripherals", {}) or {}
        # HAL 外设名（hal_conf 模块启用）——去实例号 + USART→UART / FSMC→SRAM 特例
        # （「」：build_standard_project 此前直接传 USART1，
        #   生成错误的 HAL_USART1_MODULE_ENABLED，导致 UART_HandleTypeDef 未定义）
        hal_peris: list[str] = []
        for s in periphs.values():
            p = str(s.get("periph", "") or "")
            if p:
                _base = re.sub(r"\d+$", "", p.upper())
                hal_peris.append({"USART": "UART", "FSMC": "SRAM"}.get(_base, _base))
        # main.c 外设头文件 include（三层架构：main.c 只调 App_Loop，需 include app_main.h）
        peripheral_includes = ['#include "app_main.h"'] + [
            f'#include "{fname}.h"' for fname in periphs
        ]
        # 业务层 app_business.c 引用外设句柄，需 include 各 periph.h（不含 app_main.h）
        business_includes = [f'#include "{fname}.h"' for fname in periphs]
        # 业务逻辑（原 main.c while(1) 的 main_loop）搬到业务层 App_Business_Run()
        business_body = "\n".join(slices.get("main_loop", []) or [])
        # 系统保命 loop（看门狗喂狗等）提升到应用层 App_Loop 无条件区（不被启动门挡住）
        system_body = "\n".join(slices.get("system_loop", []) or [])
        system_includes = "\n".join(
            f'#include "{fname}.h"' for fname in (slices.get("system_periphs", []) or [])
        )
        # 关闭动作（有源器件「关」）：启动门 toggle 到关闭沿时复位有源输出（蜂鸣器停/灯灭）
        off_body = "\n".join(slices.get("off_body", []) or [])
        # 防御需求（「」）：功能模板 defense 声明收集，按声明绑防御件。
        # 启动门（startup_gate）只对「有有源器件（off_body 非空）」的工程注入：
        # 有源器件（蜂鸣器/灯/报警）上电会响/亮，必须按键门；纯无源回传工程（RNG/CRC/
        # DMA/定时器/DAC）无源无噪音，上电即回传，不注入启动门——否则自动测试无法按 KEY0，
        # 门控内回传永远不跑（上板）。
        defense_units = tuple(slices.get("defense_units", []) or [])
        if off_body.strip():
            defense_units = defense_units + ("startup_gate",)

        # 中断 handler 编辑线路（「」）：
        # 功能模板 init 里已 EnableIRQ，但对应的 XXX_IRQHandler 从没生成——此处从
        # 渲染后 slot 提取「使能了哪些中断 + 各外设 handle」，按范式级材料生成
        # handler 定义/原型/extern，补上「中断来了跳 Default_Handler 死循环」的缺口。
        from knowledge.template_forge.irq_handler_gen import extract_irq_handlers

        irq_handlers, irq_protos, irq_externs = extract_irq_handlers(slices)

        files = {
            f"{base}/Core/Inc/main.h": self.generate_main_h([]),
            f"{base}/Core/Inc/{family_prefix}_hal_conf.h": self.generate_hal_conf_h(hal_peris),
            f"{base}/Core/Inc/{it_h_file}": self.generate_it_h(irq_protos),
            f"{base}/Core/Src/main.c": self.generate_main_c(
                peripheral_includes=peripheral_includes,
                peripheral_inits=list(slices.get("main_inits", []) or []),
                # 三层架构：main.c 的 while(1) 只调应用层 App_Loop()，
                # 业务逻辑搬到 app_business.c 的 App_Business_Run()（依赖方向单向）
                main_loop_body=["    App_Loop();"],
            ),
            f"{base}/Core/Src/{family_prefix}_hal_msp.c": self.generate_hal_msp_c(periphs),
            f"{base}/Core/Src/{it_c_file}": self.generate_it_c(irq_externs, irq_handlers),
            f"{base}/Core/Src/{system_c_file}": self.generate_system(),
            f"{base}/Core/Startup/{family.startup_pattern}": self.generate_startup_s(),
            f"{base}/{self._linker_file_name()}": self.generate_linker_ld(),
            # ---- 三层架构（应用层 app_main + 业务层 app_business）----
            f"{base}/Core/Inc/app_main.h": APP_MAIN_H_TEMPLATE,
            f"{base}/Core/Src/app_main.c": APP_MAIN_C_TEMPLATE.replace(
                "__SYSTEM_INCLUDES__", system_includes
            ).replace("__SYSTEM_LOOP_BODY__", system_body),
            f"{base}/Core/Inc/app_business.h": APP_BUSINESS_H_TEMPLATE,
            f"{base}/Core/Src/app_business.c": self.generate_app_business_c(
                business_includes, business_body, defense_units, off_body
            ),
            f"{base}/project_info.md": self.generate_project_info(
                project_name, list(periphs.keys()), list(periphs.keys()), ts
            ),
            f"{base}/Makefile": self.generate_makefile(),
            # 注册清单元数据（串口车间标准接口，schema 见 serial_workshop/manifest_schema.json）
            f"{base}/register_manifest.json": self._build_manifest(slices),
        }
        # 外设独立文件（gpio.c/gpio.h、usart.c/usart.h ...）
        for fname, slot in periphs.items():
            files[f"{base}/Core/Src/{fname}.c"] = self.generate_periph_c(fname, slot)
            files[f"{base}/Core/Inc/{fname}.h"] = self.generate_periph_h(fname, slot)

        # 防御件注入（「」）：按 slices 收集的 defense 需求
        # 注入对应防御件文件——走到哪个环节需要哪个防御件就绑哪个，不是全局默认开。
        from knowledge.template_forge.defense_injector import inject_defense_files

        inject_defense_files(files, base, units=defense_units)

        # 探针注入（可选，「闭环」最后一公里）
        if enable_trace:
            from serial_workshop.probes.expectations import build_expectations

            from infrastructure.config import CHIPS_DIR
            from knowledge.template_forge.trace_injector import apply_trace

            chip_name = str(getattr(self.profile, "chip_name", ""))
            exp = build_expectations(chip_name, CHIPS_DIR)
            # ID 自报：芯片型号 + 固件版本（串口车间据此识别板子）
            files = apply_trace(files, base, exp, True, chip=chip_name, fw=_tool_version())

        return files

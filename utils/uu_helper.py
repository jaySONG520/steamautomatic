import os
import time
from traceback import print_exc

from colorama import Fore, Style

import uuyoupinapi
from utils.logger import PluginLogger, handle_caught_exception
from utils.static import UU_TOKEN_FILE_PATH
from utils.tools import get_encoding

logger = PluginLogger("UULoginSolver")


def get_valid_token_for_uu(steam_client, proxies=None):
    if proxies:
        logger.info("检测到Steam代理设置，正在为悠悠有品设置相同的代理...")
    logger.info("正在为悠悠有品获取有效的token...")
    if os.path.exists(UU_TOKEN_FILE_PATH.format(steam_username=steam_client.username)):
        with open(UU_TOKEN_FILE_PATH.format(steam_username=steam_client.username), "r", encoding=get_encoding(UU_TOKEN_FILE_PATH.format(steam_username=steam_client.username))) as f:
            try:
                token = f.read().strip()
                uuyoupin = uuyoupinapi.UUAccount(token, proxy=proxies)
                logger.info("悠悠有品成功登录, 用户名: " + uuyoupin.get_user_nickname())
                return token
            except Exception as e:
                print_exc()
                logger.warning("缓存的悠悠有品Token无效")
    else:
        logger.info("未检测到存储的悠悠token")
    logger.info("即将重新登录悠悠有品！")
    token = str(get_token_automatically(proxies))
    try:
        uuyoupin = uuyoupinapi.UUAccount(token, proxy=proxies)
        logger.info("悠悠有品成功登录, 用户名: " + uuyoupin.get_user_nickname())
        with open(UU_TOKEN_FILE_PATH.format(steam_username=steam_client.username), "w", encoding="utf-8") as f:
            f.write(token)
        logger.info("悠悠有品Token已自动缓存到本地")
        return token
    except TypeError:
        logger.error("获取Token失败！可能是验证码填写错误或者未发送验证短信！")
        return False
    except Exception as e:
        handle_caught_exception(e, "[UULoginSolver]")
        return False


def get_token_automatically(proxies=None):
    """
    引导用户输入手机号，发送验证码，输入验证码，自动登录，并且返回token
    :return: token
    """
    device_info = uuyoupinapi.generate_random_string(10)
    headers = uuyoupinapi.generate_headers(device_info, device_info)

    phone_number = input(f"{Style.BRIGHT + Fore.RED}请输入手机号(+86)(如果此时有其它插件输出请忽略！输入完按回车即可！)：{Style.RESET_ALL}")
    token_id = device_info
    logger.debug("随机生成的token_id：" + token_id)
    uk = uuyoupinapi.UUAccount.get_uu_uk()
    if not uk:
        logger.warning("获取UK失败，将使用默认配置")
    result = uuyoupinapi.UUAccount.send_login_sms_code(phone_number, token_id, headers=headers, uk=uk, proxies=proxies)
    response = {}
    # 检查是否需要图形校验或手动发送短信（保留你的改进逻辑）
    msg = result.get('Msg', '')
    needs_graphical_verification = '图形' in msg or '图形校验' in msg or '图形验证' in msg
    needs_manual_sms = result.get("Code") == 5050 and needs_graphical_verification
    
    if needs_graphical_verification and not needs_manual_sms:
        # 需要图形验证码的情况 - 自动切换到手动发送短信方式（你的改进）
        logger.warning("发送验证码结果：" + msg)
        logger.info(f"{Style.BRIGHT+Fore.YELLOW}检测到需要图形校验，程序将自动切换到手动发送短信的登录方式{Style.RESET_ALL}")
        logger.info(f"{Style.BRIGHT+Fore.YELLOW}注意：你不需要手动完成图形验证，只需按照下面的提示发送短信即可{Style.RESET_ALL}")
        logger.info("正在获取手动发送短信的配置信息...")
        sms_config_result = uuyoupinapi.UUAccount.get_smsUpSignInConfig(headers, proxies).json()
        if sms_config_result.get("Code") == 0:
            logger.info("请求结果：" + sms_config_result.get("Msg", ""))
            sms_content = sms_config_result['Data'].get('SmsUpContent', '')
            sms_number = sms_config_result['Data'].get('SmsUpNumber', '')
            logger.info(f"{Style.BRIGHT+Fore.YELLOW}========== 重要提示 =========={Style.RESET_ALL}")
            logger.info(f"{Style.BRIGHT+Fore.YELLOW}这不是接收验证码，而是需要你主动发送短信！{Style.RESET_ALL}")
            logger.info(f"{Style.BRIGHT+Fore.RED}请使用手机编辑并发送以下内容：{Style.RESET_ALL}")
            logger.info(f"{Style.BRIGHT+Fore.CYAN}短信内容：{Fore.WHITE}{sms_content}{Style.RESET_ALL}")
            logger.info(f"{Style.BRIGHT+Fore.CYAN}发送到号码：{Fore.WHITE}{sms_number}{Style.RESET_ALL}")
            logger.info(f"{Style.BRIGHT+Fore.YELLOW}============================{Style.RESET_ALL}")
            logger.info(f"{Style.BRIGHT+Fore.CYAN}发送完成后，请按回车键继续...{Style.RESET_ALL}")
            input()
            logger.info("请稍候，正在验证...")
            time.sleep(3)  # 防止短信发送延迟
            response = uuyoupinapi.UUAccount.sms_sign_in(phone_number, "", token_id, headers=headers, proxies=proxies)
        else:
            logger.error("获取手动发送短信配置失败，请稍后重试或联系开发者")
            return False
    elif needs_manual_sms:
        # 需要手动发送短信的情况（Code == 5050 且包含图形）
        logger.info("该手机号需要手动发送短信进行验证，正在获取相关信息...")
        sms_config_result = uuyoupinapi.UUAccount.get_smsUpSignInConfig(headers, proxies).json()
        if sms_config_result.get("Code") == 0:
            logger.info("请求结果：" + sms_config_result.get("Msg", ""))
            sms_content = sms_config_result['Data'].get('SmsUpContent', '')
            sms_number = sms_config_result['Data'].get('SmsUpNumber', '')
            logger.info(f"{Style.BRIGHT+Fore.YELLOW}========== 重要提示 =========={Style.RESET_ALL}")
            logger.info(f"{Style.BRIGHT+Fore.YELLOW}这不是接收验证码，而是需要你主动发送短信！{Style.RESET_ALL}")
            logger.info(f"{Style.BRIGHT+Fore.RED}请使用手机编辑并发送以下内容：{Style.RESET_ALL}")
            logger.info(f"{Style.BRIGHT+Fore.CYAN}短信内容：{Fore.WHITE}{sms_content}{Style.RESET_ALL}")
            logger.info(f"{Style.BRIGHT+Fore.CYAN}发送到号码：{Fore.WHITE}{sms_number}{Style.RESET_ALL}")
            logger.info(f"{Style.BRIGHT+Fore.YELLOW}============================{Style.RESET_ALL}")
            logger.info(f"{Style.BRIGHT+Fore.CYAN}发送完成后，请按回车键继续...{Style.RESET_ALL}")
            input()
            logger.info("请稍候，正在验证...")
            time.sleep(3)  # 防止短信发送延迟
            response = uuyoupinapi.UUAccount.sms_sign_in(phone_number, "", token_id, headers=headers, proxies=proxies)
        else:
            logger.error("获取手动发送短信配置失败，请稍后重试")
            return False
    elif "成功" in msg:
        # 正常情况：直接发送短信验证码
        logger.info("发送验证码结果：" + msg)
        sms_code = input(f"{Style.BRIGHT + Fore.RED}请输入验证码(如果此时有其它插件输出请忽略！输入完按回车即可！)：{Style.RESET_ALL}")
        response = uuyoupinapi.UUAccount.sms_sign_in(phone_number, sms_code, token_id, headers=headers, proxies=proxies)
    else:
        # 其他情况，使用原来的逻辑
        logger.info("该手机号需要手动发送短信进行验证，正在获取相关信息...")
        sms_config_result = uuyoupinapi.UUAccount.get_smsUpSignInConfig(headers, proxies).json()
        if sms_config_result.get("Code") == 0:
            logger.info("请求结果：" + sms_config_result.get("Msg", ""))
            logger.info(
                f"{Style.BRIGHT + Fore.RED}请编辑发送短信 {Fore.YELLOW + sms_config_result['Data']['SmsUpContent']} {Fore.RED}到号码 {Fore.YELLOW + sms_config_result['Data']['SmsUpNumber']} {Fore.RED}！(如果此时有其它插件输出请忽略)发送完成后请按下回车{Style.RESET_ALL}",
            )
            input()
            logger.info("请稍候...")
            time.sleep(3)  # 防止短信发送延迟
            response = uuyoupinapi.UUAccount.sms_sign_in(phone_number, "", token_id, headers=headers, proxies=proxies)
    logger.info("登录结果：" + response["Msg"])
    try:
        got_token = response["Data"]["Token"]
    except (KeyError, TypeError, AttributeError):
        return False
    return got_token

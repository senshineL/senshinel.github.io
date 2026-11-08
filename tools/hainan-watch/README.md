# 海航经济舱监控（Mac 本地通知）

监控指定日期、指定海航国内直达航班：只有**官网当时最低价的普通成人经济舱**中，存在定级积分比例至少 50% 的舱位，才通过 macOS 通知中心提醒。默认每 900 秒检查一次。

默认行程：2026-11-09，HU7711，北京首都 PEK → 深圳 SZX。

按需运行：用户明确要求开始时才启动，要求停止时结束进程。默认间隔 15 分钟，可按用户指定的时间修改 `interval_seconds`；已经运行时，先停止原进程再按新间隔重新启动。

## 安装与开始

在本文件所在目录打开终端：

```sh
./setup.command
.venv/bin/python hainan_watch.py check
.venv/bin/python hainan_watch.py test-notification
./start.command
```

`setup.command` 创建本项目的虚拟环境并安装 Playwright，优先用已安装的 Edge，其次 Chrome；没有时下载 Chromium。需要 Python 3.9 或以上。初次安装需要网络。

通知使用 macOS 自带的 `osascript`，无须账号、授权码或额外通知服务。测试通知会尝试显示横幅并播放 Glass 提示音；符合条件时显示航班号、日期、最低票价和舱位。

如果没有看见测试通知，请查看通知中心，并在“系统设置 → 通知”中允许对应通知来源显示横幅和声音；专注模式可能隐藏通知。命令成功表示请求已提交给系统，仍需实际确认通知显示。

`check` 只查询，不发送通知。`watch` / `start.command` 直接开始监控，不需要配置邮箱。当前没有开启后台监控。

`start.command` 在终端前台运行，并用 `caffeinate -i` 防止 Mac 因空闲而睡眠。保持终端开启和网络连接；合盖进入睡眠、关机、强制睡眠或断网会中断检查。Ctrl+C 停止。需要提前两天监控时，在出发前两天启动即可；当前版本不自动预约未来启动。

## 修改行程

编辑 `config.json`：

```json
{
  "flight_number": "HU7711",
  "departure_date": "2026-11-09",
  "origin": "PEK",
  "destination": "SZX",
  "interval_seconds": 900,
  "minimum_qualifying_rate": "0.50",
  "browser_channel": "msedge"
}
```

返程改为实际海航航班号及 `SZX` → `PEK`。修改后重新启动。一个目录同时只运行一个监控实例。

若官网拒绝访问或页面改变，可执行 `.venv/bin/python hainan_watch.py check --headed` 查看独立浏览器。脚本不自动处理验证码。浏览器查询完成后会自动关闭。

## 条件与提醒

- 先在所有基础经济舱产品中找最低价，再检查最低价中的舱位积累比例。U 舱 670 元、E 舱 920 元时不会提醒；最低价 V/N 舱 900 元时提醒；若 U 和 N 同为最低价，提醒 N 舱。
- 同一价格和合格舱位持续存在时不重复发送；价格或合格舱位变化时发送新提醒；从不符合重新变为符合时重新提醒。
- 按飞行日匹配官方定级积分规则，不按舱位字母排序。当前规则中 N/V 为 50%，B/P 为零。规则从海航官网动态获取；获取失败、未知最低价舱位、明细最低价与列表最低价不符时停止本轮判断。
- 连续三次查询或通知失败时，尝试显示一次本地异常通知。日志保存在 `.local/watch.log`，最新查询保存在 `.local/last_check.json`。系统通知被禁用或被专注模式隐藏时，脚本无法判断你是否看到了提醒。
- 到最近一次查询取得的起飞时间自动停止。航班改期或延误后应重新核对行程配置。

## 数据范围

比较的是**海航官网、未登录、单程直达、1 位普通成人、基础经济舱、不含税费票价**。当前已验证的官网结构为每个票价系列的基础经济舱行，并要求明细最低价与航班列表展示最低价一致。

会员、学生、军人等有资格限制的专享价格，以及另加权益的套装不纳入本监控的普通成人基础票价比较。APP、会员身份及优惠券可能出现不同价格。提醒不锁价；收到后在购票渠道确认舱位、含税总价及特殊产品的积累限制。

源码通过正常浏览器填写官网搜索表单并读取结果，不直接调用未经验证的票价接口，不自动下单。

查询记录、去重状态和日志保存在本机 `.local/`；`.local/`、`.venv/` 已由此目录的 `.gitignore` 忽略。

## 来源与验证

- [海航官网查询](https://new.hnair.com/hainanair/ibe/common/flightSearch.do)
- [官方定级积分说明](https://ffp.hnair.com/FFPClub/cn/hqjf/hkjl/gszl/201809/t20180905_24975.html)
- 官方说明页使用的规则数据：`https://ffp.hnair.com/FFPClub/baseInfoController/queryQpQs?carrier=HU&segtyp=D`
- 单元测试：`python3 -m unittest -v test_hainan_watch.py`

实测及通知验证状态见 `VALIDATION.md`。

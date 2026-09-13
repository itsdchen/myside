#pragma once

/*
Some basic enums. If we start making more let's break this into smaller files.

*/

namespace pktrade {

enum class Colo {
  AWSTokA,
  // BinanceSPOT. Still not sure if Perps are elsewher.
  AWSTokC,
};

// Right now, not going to support stuff like market orders or icebergs.
// What are we calculating our pnl in?
// If we set risk limits, should set them according to that.
enum class BaseCurrency {
  BTC,
  // Perps can be settled with USD.
  USD,
  USDT,
  USDC,
  BNB,
  BUSD,
  TUSD,
  UNSET,
};

// Which slot a multiplier writes into. Each writer owns its own slot so it can't
// clobber another's, eg. a blanket Usermsg size mult ("halve everything") scales
// a trader relative to its conf'd size instead of replacing it.
//
// Each slot is assigned absolutely, so setting the same slot twice is idempotent
// (it does not compound). How the slots combine differs by multiplier:
//   - size mults are multiplied (setSizeMult): sources scale each other.
//   - thresh mults take the max (setThreshMult): the widest wins, so the two
//     defensive wideners don't compound into something wider than either meant.
// Not every source applies to every multiplier; setters reject the ones they
// don't handle.
enum class MultSource {
  // From a usermsg. Applies to both size and thresh.
  Usermsg,
  // From the strat conf file (eg. pktrader's size_mult), applied at startup.
  // Size only.
  Conf,
  // From the riskman's double-down detector sizing up on sustained edge. Size only.
  DoubleDown,
  // From the riskman's bleed detector widening/recovering. Thresh only.
  Bleed,
  // From an ordex coming back out of minfv. Thresh only.
  MinFv,
};

// This must match UM_ID_MAP in usermsg.py
enum class UserMsgId {
  UNSET = 1,

  // gatewaCAE-level calls start at 100
  SET_GATEWAY_TRADING = 100,

  // PKTrader-level calls start at 200
  SET_PK_CAN_TRADE = 200,
  SET_PK_MAX_SHORTABLE = 201,
  SET_PK_MIN_PNL = 202,
  // SET_TRADING_LEVEL = 203 // args:{tl:int}
  // REFRESH_CONF = 204 // args:{conf:string}

  // Ordex-level calls start at 300
  SET_PLACE_THRESH = 301, // args:{thresh:double}, must be singular
  SET_THRESH_MULT = 302, // args:{mult:double}
  SET_ORDER_SIZE = 303, // args:{size:double}
  SET_SIZE_MULT = 304, // args:{mult:double}
  SET_RELATIVE_BETA = 305, // args:{beta:double}
  SET_EXTRA_THRESHES = 306, // args:{buy:double,sell:double}
  SET_CONST_PRED_PX = 307, // args:{px:double}

  // Risk-level calls start at 400
  // RESET_MINFV = 401, // args:{min_pnl:double,fv_limit:int,timeout_minfv:int}
  FORCE_INHERIT = 402, // args:{pos:double}
  SET_TL = 403, // args:{tl:int}
  FORCE_INHERIT_LOOP = 404, // args:{} (no args, triggers full inherit calc)
  SET_TL_REASON = 405, // args:{tl:int,reason:string}

};

} // namespace pktrade

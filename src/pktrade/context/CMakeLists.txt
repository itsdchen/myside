add_pk_library(
  context
  global_vars.cc
  live_context.cc
  local_context.cc
  local_crypto.cc
  local_gateways/local_binance_gw.cc
)

target_link_libraries(
  context
  PRIVATE mktdata
          risk
          signals
          tempos
          cpr::cpr
          ixwebsocket::ixwebsocket
)

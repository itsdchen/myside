add_pk_library(
  ordex
  ordex.cc
  ordex_factory.cc
  alpha_rel_wide_mm.cc
  diagnostics_mm.cc
  trade_recorder.cc
  rel_cross.cc
  rel_wide_mm_2.cc
  wide_mm.cc
  widgets/hedge_positioner.cc
)

target_link_libraries(
  ordex
  PUBLIC mktdata context
  PRIVATE signals
          pkcore
          ixwebsocket::ixwebsocket
          cpr::cpr
          nlohmann_json::nlohmann_json
)

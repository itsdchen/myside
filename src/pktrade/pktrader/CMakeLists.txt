add_pk_library(pkt pktrader.cc trade_carrier.cc um_handler.cc)

target_link_libraries(
  pkt
  PUBLIC mktdata
         ordex
         tempos
         pkcore
         risk
         signals
         sim
         tempos
         util
         pkcore
  PRIVATE
    cpr::cpr
    nlohmann_json::nlohmann_json

  )

add_pk_executable(pktrade trade_main.cc)

target_link_libraries(pktrade PRIVATE pkt util)
add_dependencies(pktrade generate_version)

add_pk_library(risk commissioner.cc trade_risk_man.cc global_positioner.cc)

target_link_libraries(
  risk
  PUBLIC pkcore fmt::fmt util pkt
  PRIVATE
  cpr::cpr
)

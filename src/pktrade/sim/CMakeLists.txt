add_pk_library(sim sim_verse.cc sim_lvl_inst.cc sim_base.cc)

target_link_libraries(
  sim
  PUBLIC mktdata context
  PRIVATE pkcore cpr::cpr
)

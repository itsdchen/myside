add_pk_library(mktdata md_beacon.cc)

target_link_libraries(
  mktdata
  PUBLIC pkcore
  PRIVATE
)

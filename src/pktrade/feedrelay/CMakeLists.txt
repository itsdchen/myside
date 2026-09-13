add_pk_executable(pkrelay pkrelay.cc)
target_link_libraries(
  pkrelay
  PRIVATE glog::glog
          RapidJSON::RapidJSON
          fmt::fmt
          databento::databento
          pkcore
          util
)

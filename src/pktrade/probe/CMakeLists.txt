add_pk_executable(pkprobe_send pkprobe_send.cc)
target_link_libraries(
  pkprobe_send
  PRIVATE glog::glog
          RapidJSON::RapidJSON
          fmt::fmt
          pkcore
          util
)

add_pk_executable(pkprobe_recv pkprobe_recv.cc)
target_link_libraries(
  pkprobe_recv
  PRIVATE glog::glog
          RapidJSON::RapidJSON
          fmt::fmt
          pkcore
          util
)

add_pk_executable(pkmultifeed pkmultifeed.cc)
target_link_libraries(
  pkmultifeed
  PRIVATE glog::glog
          ixwebsocket::ixwebsocket
          RapidJSON::RapidJSON
          cppzmq::cppzmq
          nlohmann_json::nlohmann_json
          cpr::cpr
          databento::databento
          pkproto
          pkcore
          util
)

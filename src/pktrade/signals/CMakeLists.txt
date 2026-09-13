add_pk_library(
  signals
  signal.cc
  signal_factory.cc
  sig_mid.cc
  sig_wmid.cc
  sig_linear.cc
  sig_der.cc
  sig_trd_impulse.cc
  sig_trd_px.cc
  sig_book_impulse.cc
  sig_deep_book.cc
  sig_liq_balance.cc
  sig_libra.cc
  sig_ret_ems.cc
  sig_file.cc
)

target_link_libraries(
  signals
  PUBLIC mktdata context pkcore
  PRIVATE
)
